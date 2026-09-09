# Lazy annotations: required for the `client = None` fallback below — without
# this, module-level annotations like `client.CoreV1Api | None` are evaluated
# at import time and crash when kubernetes is not installed (local runs/tests).
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from prometheus_client import Counter, Gauge, start_http_server

try:
    from kubernetes import client, config
    from kubernetes.client import ApiException
except Exception:  # pragma: no cover - optional dependency for local runs
    client = None  # type: ignore[assignment]
    config = None  # type: ignore[assignment]
    ApiException = Exception  # type: ignore[assignment]

# mount_name -> mount_path, the two labels every af_node_mount_* series
# carries. Everything else about a probe (check file, checksum, fio target,
# volumes) belongs to the DaemonSet that runs it —
# apps/monitoring/af-monitoring/daemonset-af-node-probe.yaml. Keeping one copy
# of that config means it cannot drift; tests/manifests/test_node_probes.py
# holds this dict and those DaemonSets to the same set of mounts.
MOUNTS: Dict[str, str] = {
    "/depot/": "/depot/",
    "/work/": "/work/",
    "eos": "eos",
    "cvmfs": "cvmfs",
}

PING_TIMEOUT_S = float(os.getenv("PING_TIMEOUT_S", "3"))
METADATA_TIMEOUT_S = float(os.getenv("METADATA_TIMEOUT_S", "10"))
FIO_TIMEOUT_S = float(os.getenv("FIO_TIMEOUT_S", "120"))

CHECK_INTERVAL_S = float(os.getenv("CHECK_INTERVAL_S", "600"))
RESULTS_DIR = Path(os.getenv("RESULTS_DIR", "/af-node-monitor/results"))

POD_NAMESPACE = os.getenv("POD_NAMESPACE", "default")


def _vlog(msg: str) -> None:
    if os.getenv("AF_NODE_MONITOR_VERBOSE", "").lower() in ("1", "true", "yes"):
        print(msg)


def _elog(msg: str) -> None:
    # Always emit errors, even when verbose logging is disabled.
    print(msg)


# Cadence the probe DaemonSets write at (probe_agent.PROBE_INTERVAL_S). The
# exporter does not drive it; it only needs to agree on what "recent" means.
PROBE_INTERVAL_S = float(os.getenv("PROBE_INTERVAL_S", "600"))

RESULT_STALE_WINDOW_S = float(
    os.getenv("RESULT_STALE_WINDOW_S", str(3 * PROBE_INTERVAL_S))
)

# A result stamped in the future is a clock-skewed node, not a fresh check.
# Without this a skewed node can never go stale, so a dead mount there would
# stay green forever.
CLOCK_SKEW_TOLERANCE_S = float(os.getenv("CLOCK_SKEW_TOLERANCE_S", "300"))

# Reads of the results PVC are bounded: it is CephFS, and a read against a
# wedged mount is uninterruptible. An unbounded one would freeze this loop and
# take every mount on every node down to "unknown" at once.
RESULTS_READ_TIMEOUT_S = float(os.getenv("RESULTS_READ_TIMEOUT_S", "15"))

NODE_CACHE_TTL_S = float(os.getenv("NODE_CACHE_TTL_S", "300"))


try:
    mount_valid = Gauge(
        "af_node_mount_valid",
        "Storage mount health",
        ["mount_name", "mount_path", "node", "node_pool"],
    )
    mount_ping_ms = Gauge(
        "af_node_mount_ping_ms",
        "Storage mount ping time in milliseconds",
        ["mount_name", "mount_path", "node", "node_pool"],
    )
    mount_data_rate_gbps = Gauge(
        "af_node_mount_data_rate_gbps",
        "Storage mount sequential read throughput in Gbps",
        ["mount_name", "mount_path", "node", "node_pool"],
    )
    mount_metadata_latency_ms = Gauge(
        "af_node_mount_metadata_latency_ms",
        "Storage mount metadata latency in milliseconds (ls)",
        ["mount_name", "mount_path", "node", "node_pool"],
    )

    mount_result_fresh = Gauge(
        "af_node_mount_result_fresh",
        "1 when af_node_mount_valid reflects a recent completed check on a Ready "
        "node, 0 when the node is Ready but no usable result exists (unknown). "
        "Series are absent (null) for NotReady nodes",
        ["mount_name", "mount_path", "node", "node_pool"],
    )

    mount_timeout_total = Counter(
        "af_node_mount_timeout_total",
        "Total number of timeouts contacting mount workers or running checks",
        ["mount_name", "mount_path", "node", "node_pool", "check_type"],
    )
    mount_last_success_ts = Gauge(
        "af_node_mount_last_success_timestamp_seconds",
        "Unix timestamp of last successful metrics update for mount",
        ["mount_name", "mount_path", "node", "node_pool"],
    )
    mount_probe_up = Gauge(
        "af_node_mount_probe_up",
        "1 when the probe DaemonSet pod for this mount/node is Ready, 0 when "
        "it is missing or not Ready. Separates a broken mount (valid=0) from "
        "a broken probe — both otherwise surface only as unknown",
        ["mount_name", "mount_path", "node", "node_pool"],
    )

    monitor_last_iteration_ts = Gauge(
        "af_node_monitor_last_iteration_timestamp_seconds",
        "Unix timestamp of last completed metrics iteration",
    )
    monitor_results_available = Gauge(
        "af_node_monitor_results_available",
        "1 when the results PVC could be read this iteration. On 0 every "
        "af_node_mount_* series goes absent, which no mount alert can see",
    )
except Exception as e:  # pragma: no cover - defensive
    print(f"Error defining Prometheus metrics: {e}")


def _timeout_ping_ms() -> float:
    return PING_TIMEOUT_S * 1000.0


def _timeout_metadata_ms() -> float:
    return METADATA_TIMEOUT_S * 1000.0


def _sanitized_mount_name(name: str) -> str:
    return name.strip("/").replace("/", "_") or "root"


def _sanitized_node_name(name: str) -> str:
    return name.strip().replace("/", "_") if name else ""


_core_v1: client.CoreV1Api | None  # type: ignore[type-arg]
_batch_v1: client.BatchV1Api | None  # type: ignore[type-arg]
_k8s_ready: bool = False

# All AF-labelled nodes as (name, pool, ready). Metrics must cover NotReady
# nodes too: otherwise last-known-good gauges freeze green while probes cannot
# run.
_af_nodes_cache: List[tuple[str, str, bool]] = []
_last_node_refresh: float = 0.0


def _init_k8s() -> None:
    global _core_v1, _batch_v1, _k8s_ready
    if _k8s_ready or client is None or config is None:
        return
    try:
        # Prefer in-cluster config; fall back to kubeconfig for local testing.
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        _core_v1 = client.CoreV1Api()
        _batch_v1 = client.BatchV1Api()
        _k8s_ready = True
        _vlog("[node_healthcheck] Kubernetes client initialized")
    except Exception as e:  # pragma: no cover - defensive
        print(f"[node_healthcheck] Failed to initialize Kubernetes client: {e}")
        _k8s_ready = False


def _result_path(mount_name: str, node_name: str) -> Path:
    mount_key = _sanitized_mount_name(mount_name)
    node_key = _sanitized_node_name(node_name)
    if node_key:
        return RESULTS_DIR / f"{mount_key}__{node_key}.json"
    return RESULTS_DIR / f"{mount_key}.json"


def _call_bounded(fn: Any, timeout_s: float) -> tuple[bool, Any]:
    """Run fn in a throwaway thread; return (completed, value).

    A read against a wedged CephFS mount sits in uninterruptible sleep, so the
    thread is abandoned rather than joined — it is a daemon and unblocks when
    the filesystem does. Callers must stop reading after the first timeout so
    at most one thread leaks per iteration.
    """
    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # re-raised on the calling thread
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return False, None
    if "error" in box:
        raise box["error"]
    return True, box.get("value")


def _read_result_file(path: Path) -> Dict[str, Any] | None:
    with path.open("r", encoding="utf-8") as f:
        loaded: Dict[str, Any] = json.load(f)
        return loaded


def _load_result(mount_name: str, node_name: str) -> Dict[str, Any] | None:
    path = _result_path(mount_name, node_name)
    try:
        done, value = _call_bounded(
            lambda: _read_result_file(path), RESULTS_READ_TIMEOUT_S
        )
    except FileNotFoundError:
        # Per-node result file not present yet.
        return None
    except OSError as e:
        # Underlying storage (PVC) likely unavailable; signal caller to skip metrics.
        print(
            f"[node_healthcheck] Storage error reading result for {mount_name} "
            f"from {path}: {e}"
        )
        return {"_storage_error": True}
    except Exception as e:
        print(f"Error reading result for {mount_name} from {path}: {e}")
        return None

    if not done:
        _elog(
            f"[node_healthcheck] Read of {path} did not return in "
            f"{RESULTS_READ_TIMEOUT_S}s; results storage is wedged"
        )
        return {"_storage_error": True, "_timed_out": True}
    return value  # type: ignore[no-any-return]


# node name -> "prod" | "dev", filled by _refresh_node_caches()
_node_pools: Dict[str, str] = {}


def _node_is_ready(node: Any) -> bool:
    conditions = getattr(getattr(node, "status", None), "conditions", None) or []
    for cond in conditions:
        if (
            getattr(cond, "type", "") == "Ready"
            and getattr(cond, "status", "") == "True"
        ):
            return True
    return False


def _refresh_node_caches() -> None:
    """Refresh Ready-only and all-AF-node caches from the API."""
    _init_k8s()
    if not _k8s_ready or _core_v1 is None:
        return

    global _af_nodes_cache, _last_node_refresh
    now = time.time()
    if _af_nodes_cache and (now - _last_node_refresh) < NODE_CACHE_TTL_S:
        return

    # Both pools are monitored. The pool is recorded per node so that alerts
    # and user-facing tools can tell them apart: a dev node failing is an
    # operator's problem, not a facility outage.
    label_sets = [
        ("cms-af-prod=true", "prod"),
        ("cms-af-dev=true", "dev"),
    ]
    by_name: dict[str, tuple[str, bool]] = {}
    try:
        for selector, pool in label_sets:
            resp = _core_v1.list_node(label_selector=selector)
            for node in resp.items:
                if not node.metadata or not node.metadata.name:
                    continue
                name = node.metadata.name
                ready = _node_is_ready(node)
                # first match wins: a node labelled both is production
                if name not in by_name:
                    by_name[name] = (pool, ready)
    except ApiException as e:  # type: ignore[misc]
        print(f"[node_healthcheck] Error listing nodes: {e}")
        return
    except Exception as e:  # pragma: no cover - defensive
        print(f"[node_healthcheck] Unexpected error listing nodes: {e}")
        return

    # Drop gauges for the inactive pool (and for nodes that left the AF set).
    # A leftover node_pool series with the 10s timeout sentinel is what made
    # paf-b00 look red on heatmaps while its live checks were fine.
    other_pool = {"prod": "dev", "dev": "prod"}
    prev_pools = dict(_node_pools)
    for name, (pool, _ready) in by_name.items():
        _clear_node_pool_gauges(name, other_pool[pool])
    for name, old_pool in prev_pools.items():
        if name not in by_name:
            _clear_node_pool_gauges(name, old_pool)

    _node_pools.clear()
    _node_pools.update({name: pool for name, (pool, _ready) in by_name.items()})

    _af_nodes_cache = sorted(
        ((name, pool, ready) for name, (pool, ready) in by_name.items()),
        key=lambda row: row[0],
    )
    _last_node_refresh = now


def _list_af_nodes() -> List[tuple[str, str, bool]]:
    """Return all AF-labelled nodes as (name, pool, ready).

    NotReady nodes are included so their last-known-good gauges can be cleared
    (null in Prometheus) rather than freezing green while probes cannot run.
    """
    _refresh_node_caches()
    if not _k8s_ready or _core_v1 is None:
        return []
    return _af_nodes_cache


RESULT_GAUGES = (
    "mount_valid",
    "mount_ping_ms",
    "mount_data_rate_gbps",
    "mount_metadata_latency_ms",
    "mount_result_fresh",
    "mount_last_success_ts",
)


def _clear_gauges(labels: dict[str, str], names: tuple[str, ...]) -> None:
    labelvalues = (
        labels["mount_name"],
        labels["mount_path"],
        labels["node"],
        labels["node_pool"],
    )
    for name in names:
        try:
            globals()[name].remove(*labelvalues)
        except KeyError:
            pass


def _clear_result_gauges(labels: dict[str, str]) -> None:
    """Drop the result-derived gauges, keeping af_node_mount_probe_up.

    Used when the results PVC cannot be read: what the check found is unknown,
    but whether a probe is running is still known and is the only thing that
    tells the two apart.
    """
    _clear_gauges(labels, RESULT_GAUGES)


def _clear_mount_gauges(labels: dict[str, str]) -> None:
    """Drop every status gauge for a mount/node so scrapes show null.

    Counters are left alone — they are cumulative and do not drive green/red.
    """
    _clear_gauges(labels, RESULT_GAUGES + ("mount_probe_up",))


def _clear_node_pool_gauges(node_name: str, pool: str) -> None:
    """Drop all mount status gauges for a node under one node_pool label.

    prometheus_client keeps every label set forever until remove(). When a
    node flips between cms-af-dev and cms-af-prod (or leaves the AF set), the
    inactive pool's series must be dropped — otherwise scrapes keep exporting
    a frozen timeout sentinel that Grafana heatmaps can sum into a false red.
    """
    for m_name, m_path in MOUNTS.items():
        _clear_mount_gauges(
            {
                "mount_name": m_name,
                "mount_path": m_path,
                "node": node_name,
                "node_pool": pool,
            }
        )


def _publish_probe_up(labels: dict[str, str], ready: bool | None) -> None:
    """None means the pod list could not be read — absent, not 0."""
    if ready is None:
        _clear_gauges(labels, ("mount_probe_up",))
        return
    mount_probe_up.labels(**labels).set(1 if ready else 0)


def _publish_unusable(labels: dict[str, str], check_type: str) -> None:
    """Ready node, but no usable check — unknown (fresh=0), not a confirmed failure.

    Confirmed failures set valid=0 with fresh=1 after a completed check. That is
    what turns dashboards/alerts red; this path must not.
    """
    mount_valid.labels(**labels).set(0)
    mount_ping_ms.labels(**labels).set(_timeout_ping_ms())
    mount_metadata_latency_ms.labels(**labels).set(_timeout_metadata_ms())
    mount_data_rate_gbps.labels(**labels).set(0.0)
    mount_result_fresh.labels(**labels).set(0)
    mount_timeout_total.labels(check_type=check_type, **labels).inc()


def _probe_pod_ready(pod: Any) -> bool:
    if getattr(pod.metadata, "deletion_timestamp", None):
        # Terminating during a rolling update — do not count it as coverage.
        return False
    status = getattr(pod, "status", None)
    if not status or getattr(status, "phase", "") != "Running":
        return False
    for cond in getattr(status, "conditions", None) or []:
        if getattr(cond, "type", "") == "Ready":
            return getattr(cond, "status", "") == "True"
    return False


def _probe_pod_states() -> Dict[tuple[str, str], bool] | None:
    """{(mount_key, node_key): ready} for every probe DaemonSet pod.

    The node comes from spec.nodeName rather than a label: one DaemonSet
    template covers every node, so it cannot carry a per-node label the way
    the old per-node Jobs did.

    None on an API failure — distinct from an empty map. Reporting every node
    as having no probe because one list call was refused would paint the whole
    fleet as unmonitored over an RBAC blip.
    """
    _init_k8s()
    if not _k8s_ready or _core_v1 is None:
        return None

    try:
        pods = _core_v1.list_namespaced_pod(
            namespace=POD_NAMESPACE,
            label_selector="app=af-node-monitor,component=probe",
        )
    except ApiException as e:  # type: ignore[misc]
        _elog(f"[node_healthcheck] Error listing probe pods: {e}")
        return None
    except Exception as e:  # pragma: no cover - defensive
        _elog(f"[node_healthcheck] Unexpected error listing probe pods: {e}")
        return None

    states: Dict[tuple[str, str], bool] = {}
    for pod in pods.items:
        labels = getattr(pod.metadata, "labels", None) or {}
        mount_key = labels.get("mount", "")
        node_key = _sanitized_node_name(getattr(pod.spec, "node_name", "") or "")
        if not mount_key or not node_key:
            continue
        key = (mount_key, node_key)
        # Two pods for one node exist briefly during a rollout; Ready wins.
        states[key] = states.get(key, False) or _probe_pod_ready(pod)
    return states


def _cleanup_legacy_jobs() -> None:
    """Delete Jobs left behind by the pre-DaemonSet exporter.

    Their TTL would clear them within minutes anyway, but a Job wedged on a
    node that cannot mount the results PVC only finishes when its deadline
    expires, and until then it keeps a doomed Pod on that node.
    """
    _init_k8s()
    if not _k8s_ready or _batch_v1 is None:
        return
    try:
        jobs = _batch_v1.list_namespaced_job(
            namespace=POD_NAMESPACE, label_selector="app=af-node-monitor"
        )
    except Exception as e:
        _elog(f"[node_healthcheck] Could not list legacy Jobs: {e}")
        return

    for job in jobs.items:
        name = getattr(job.metadata, "name", None)
        if not name:
            continue
        try:
            _batch_v1.delete_namespaced_job(
                name=name,
                namespace=POD_NAMESPACE,
                propagation_policy="Background",
            )
            print(f"[node_healthcheck] Deleted legacy Job {name}")
        except Exception as e:
            _elog(f"[node_healthcheck] Could not delete legacy Job {name}: {e}")


def update_metrics() -> None:
    now = time.time()

    af_nodes = _list_af_nodes()
    if not af_nodes:
        # Fallback: still try to read legacy per-mount results.
        af_nodes = [("", "prod", True)]
    probe_states = _probe_pod_states()

    # Set once a read of the results PVC fails to return. Every further read
    # this iteration would wedge the same way, so they are skipped and the
    # mounts reported as unreadable rather than as failing.
    storage_wedged = False
    storage_ok = True

    for m_name, mount_path in MOUNTS.items():
        mount_key = _sanitized_mount_name(m_name)
        for node_name, pool, ready in af_nodes:
            labels = {
                "mount_name": m_name,
                "mount_path": mount_path,
                "node": node_name or "unknown",
                "node_pool": pool,
            }

            # NotReady: probes cannot report. Clear gauges so Prometheus/Grafana
            # see null (gap), not last-known-good green and not a false red. Red
            # is reserved for a completed failing check on a Ready node
            # (fresh=1).
            if node_name and not ready:
                labels["node"] = node_name
                _clear_mount_gauges(labels)
                continue

            node_key = _sanitized_node_name(node_name)
            probe_ready = (
                probe_states.get((mount_key, node_key), False)
                if probe_states is not None
                else None
            )
            has_probe = (
                probe_states is not None and (mount_key, node_key) in probe_states
            )

            if storage_wedged:
                _publish_probe_up(labels, probe_ready)
                _clear_result_gauges(labels)
                continue

            data = _load_result(m_name, node_name)
            # Use node from result JSON (the probe pod's node); fallback to
            # discovery so the metric always reflects the node that produced it.
            node_for_label = ((data.get("node") or "").strip() if data else "") or (
                node_name or "unknown"
            )

            labels = {
                "mount_name": m_name,
                "mount_path": mount_path,
                "node": node_for_label,
                "node_pool": _node_pools.get(node_for_label, pool),
            }
            _publish_probe_up(labels, probe_ready)

            if data and data.get("_storage_error"):
                # Results PVC is unavailable; drop the result series so they
                # appear empty. af_node_mount_probe_up stays, and
                # af_node_monitor_results_available says why.
                storage_ok = False
                if data.get("_timed_out"):
                    storage_wedged = True
                _clear_result_gauges(labels)
                continue

            if not data:
                # No result yet: expose timeout semantics for latency/throughput
                # gauges so alerts/dashboards see an explicit failure signal.
                check_type = "no_recent_result"
                if node_key and has_probe:
                    # Distinguish the case where a probe exists but has not
                    # produced output yet (for example stuck Pending or
                    # ContainerCreating because a volume will not mount).
                    check_type = "job_never_started"
                _publish_unusable(labels, check_type)
                continue

            try:
                timestamp = float(data.get("timestamp", 0))
            except (TypeError, ValueError):
                timestamp = 0.0

            # A missing or future-dated timestamp cannot say whether the check
            # is recent. Treating it as fresh would let a skewed node hold a
            # dead mount green indefinitely.
            if timestamp <= 0 or timestamp > now + CLOCK_SKEW_TOLERANCE_S:
                _publish_unusable(labels, "bad_timestamp")
                continue

            timeout = bool(data.get("timeout", False))
            ok = bool(data.get("ok", False)) and not timeout
            stale = now - timestamp > RESULT_STALE_WINDOW_S

            if stale and ok:
                # Last success is too old — unknown, not green.
                _publish_unusable(labels, "stale_result")
                continue

            # Fresh result, or a stale *failure*/timeout: keep publishing as a
            # completed check (fresh=1). Stale EOS timeouts must stay red
            # (AFMountInvalid), not flip to unknown and disappear from MCP.

            ping_ms = data.get("ping_ms")
            meta_ms = data.get("metadata_ms")
            gbps = data.get("throughput_gbps")

            mount_valid.labels(**labels).set(1 if ok else 0)
            mount_result_fresh.labels(**labels).set(1)

            if timeout:
                # On timeout, expose worst-case latency semantics for both ping and metadata,
                # regardless of any partial measurements in the JSON.
                if ping_ms is not None:
                    mount_ping_ms.labels(**labels).set(float(ping_ms))
                else:
                    mount_ping_ms.labels(**labels).set(_timeout_ping_ms())

                mount_metadata_latency_ms.labels(**labels).set(_timeout_metadata_ms())

                mount_data_rate_gbps.labels(**labels).set(0.0)
                mount_timeout_total.labels(check_type="job_result", **labels).inc()
            else:
                if ping_ms is not None:
                    mount_ping_ms.labels(**labels).set(float(ping_ms))

                if meta_ms is not None:
                    mount_metadata_latency_ms.labels(**labels).set(float(meta_ms))

                if gbps is not None:
                    mount_data_rate_gbps.labels(**labels).set(float(gbps))

                if ok:
                    mount_last_success_ts.labels(**labels).set(timestamp)
                else:
                    mount_data_rate_gbps.labels(**labels).set(
                        float(gbps) if gbps is not None else 0.0
                    )
                    if ping_ms is None:
                        mount_ping_ms.labels(**labels).set(_timeout_ping_ms())
                    if meta_ms is None:
                        mount_metadata_latency_ms.labels(**labels).set(
                            _timeout_metadata_ms()
                        )

    monitor_results_available.set(1 if storage_ok else 0)


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    start_http_server(8000)
    _cleanup_legacy_jobs()
    while True:
        try:
            update_metrics()
            monitor_last_iteration_ts.set(time.time())
        except Exception as e:
            _elog(f"[node_healthcheck] update_metrics failed: {e}")
        time.sleep(CHECK_INTERVAL_S)
