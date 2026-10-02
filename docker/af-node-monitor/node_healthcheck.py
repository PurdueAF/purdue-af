# Lazy annotations: required for the `client = None` fallback below — without
# this, module-level annotations like `client.CoreV1Api | None` are evaluated
# at import time and crash when kubernetes is not installed (local runs/tests).
from __future__ import annotations

import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
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

# Sentinels only: the value published for ping/metadata latency when a check
# gives up. They do NOT bound anything here — the probe DaemonSets carry the
# real timeouts. Both sides are set from the manifests and
# tests/manifests/test_node_probes.py holds them equal, because a sentinel
# that disagrees with the timeout it stands for charts a latency that was
# never measured.
PING_TIMEOUT_S = float(os.getenv("PING_TIMEOUT_S", "3"))
METADATA_TIMEOUT_S = float(os.getenv("METADATA_TIMEOUT_S", "10"))

CHECK_INTERVAL_S = float(os.getenv("CHECK_INTERVAL_S", "60"))
CODE = Path(__file__)

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

NODE_CACHE_TTL_S = float(os.getenv("NODE_CACHE_TTL_S", "300"))

# Pulls are bounded per socket operation and per pass, so no probe can stall one.
FETCH_TIMEOUT_S = float(os.getenv("FETCH_TIMEOUT_S", "5"))
FETCH_PASS_TIMEOUT_S = float(os.getenv("FETCH_PASS_TIMEOUT_S", "30"))
FETCH_WORKERS = int(os.getenv("FETCH_WORKERS", "16"))
MAX_RESULT_BYTES = 64 * 1024
# Used when a probe pod declares no port named "http".
DEFAULT_PROBE_PORT = 8080

# How long a refused pod list falls back to the last good one.
POD_CACHE_TTL_S = float(os.getenv("POD_CACHE_TTL_S", "300"))

# Past these, a probe pod stuck on its volume is a mount failure, not a slow start.
MOUNT_SETUP_GRACE_S = float(os.getenv("MOUNT_SETUP_GRACE_S", "600"))
MOUNT_SETUP_RESTARTS = int(os.getenv("MOUNT_SETUP_RESTARTS", "3"))


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
        "Storage mount single-stream sequential direct read throughput in Gbps",
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
        "1 when the probe pod for this mount/node is Ready and answered this "
        "pass, 0 when it is missing, not Ready or unreachable. Separates a "
        "broken mount (valid=0) from a broken probe — both otherwise surface "
        "only as unknown",
        ["mount_name", "mount_path", "node", "node_pool"],
    )

    monitor_last_iteration_ts = Gauge(
        "af_node_monitor_last_iteration_timestamp_seconds",
        "Unix timestamp of last completed metrics iteration",
    )
    monitor_results_available = Gauge(
        "af_node_monitor_results_available",
        "Fraction of Ready probe pods that answered the exporter this pass. "
        "At 0 no mount is being checked at all, which reads as unknown, not "
        "as failing",
    )

    # Held as objects, not names: a name resolved through globals() turns a
    # typo into the KeyError that _clear_gauges swallows, and a gauge that is
    # silently never cleared is exactly the frozen last-known-good green that
    # clearing exists to prevent.
    RESULT_GAUGES = (
        mount_valid,
        mount_ping_ms,
        mount_data_rate_gbps,
        mount_metadata_latency_ms,
        mount_result_fresh,
        mount_last_success_ts,
    )
    ALL_MOUNT_GAUGES = RESULT_GAUGES + (mount_probe_up,)
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
_k8s_ready: bool = False

# All AF-labelled nodes as (name, pool, ready). Metrics must cover NotReady
# nodes too: otherwise last-known-good gauges freeze green while probes cannot
# run.
_af_nodes_cache: List[tuple[str, str, bool]] = []
_last_node_refresh: float = 0.0


def _init_k8s() -> None:
    global _core_v1, _k8s_ready
    if _k8s_ready or client is None or config is None:
        return
    try:
        # Prefer in-cluster config; fall back to kubeconfig for local testing.
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        _core_v1 = client.CoreV1Api()
        _k8s_ready = True
        _vlog("[node_healthcheck] Kubernetes client initialized")
    except Exception as e:  # pragma: no cover - defensive
        print(f"[node_healthcheck] Failed to initialize Kubernetes client: {e}")
        _k8s_ready = False


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

    # Drop gauges for the inactive pool and for nodes that left the AF set.
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


def _clear_gauges(labels: dict[str, str], gauges: tuple[Any, ...]) -> None:
    labelvalues = (
        labels["mount_name"],
        labels["mount_path"],
        labels["node"],
        labels["node_pool"],
    )
    for gauge in gauges:
        try:
            gauge.remove(*labelvalues)
        except KeyError:
            # This label set was never published; nothing to drop.
            pass


def _clear_mount_gauges(labels: dict[str, str]) -> None:
    """Drop every status gauge for a mount/node so scrapes show null.

    Counters are left alone — they are cumulative and do not drive green/red.
    """
    _clear_gauges(labels, ALL_MOUNT_GAUGES)


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
        _clear_gauges(labels, (mount_probe_up,))
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


def _publish_setup_failed(labels: dict[str, str]) -> None:
    """The probe cannot mount its volume while a sibling on the node runs.

    Only the volume differs between the two pods, and a session landing here
    carries the same volume spec, so this is a completed failing check
    (fresh=1), not an unknown.
    """
    mount_valid.labels(**labels).set(0)
    mount_ping_ms.labels(**labels).set(_timeout_ping_ms())
    mount_metadata_latency_ms.labels(**labels).set(_timeout_metadata_ms())
    mount_data_rate_gbps.labels(**labels).set(0.0)
    mount_result_fresh.labels(**labels).set(1)
    mount_timeout_total.labels(check_type="mount_setup_failed", **labels).inc()


@dataclass
class Probe:
    ready: bool = False
    ip: str = ""
    port: int = DEFAULT_PROBE_PORT
    setup_failed: bool = False


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


def _probe_port(pod: Any) -> int:
    for container in getattr(pod.spec, "containers", None) or []:
        for port in getattr(container, "ports", None) or []:
            if getattr(port, "name", "") == "http":
                return int(port.container_port)
    return DEFAULT_PROBE_PORT


def _mount_setup_failed(pod: Any, now: float) -> bool:
    """True when the pod cannot start because its volume will not mount.

    Pending with PodReadyToStartContainers=False is the kubelet still waiting
    on a volume, before any sandbox or image pull. RunContainerError and
    ContainerCannotRun are the runtime failing to bind a volume in. Both could
    also be a broken node; the caller only trusts them while a sibling probe on
    the same node answers.
    """
    if getattr(pod.metadata, "deletion_timestamp", None):
        return False
    status = getattr(pod, "status", None)
    if status is None:
        return False
    if getattr(status, "phase", "") == "Pending":
        created = getattr(pod.metadata, "creation_timestamp", None)
        waited = now - created.timestamp() if created else 0.0
        for cond in getattr(status, "conditions", None) or []:
            if (
                getattr(cond, "type", "") == "PodReadyToStartContainers"
                and getattr(cond, "status", "") == "False"
                and waited > MOUNT_SETUP_GRACE_S
            ):
                return True
    for cs in getattr(status, "container_statuses", None) or []:
        state = getattr(cs, "state", None)
        if state is not None and getattr(state, "running", None):
            continue
        waiting = getattr(state, "waiting", None) if state is not None else None
        last = getattr(cs, "last_state", None)
        terminated = getattr(last, "terminated", None) if last is not None else None
        bind_failed = getattr(waiting, "reason", "") == "RunContainerError" or (
            getattr(terminated, "reason", "") == "ContainerCannotRun"
        )
        if bind_failed and (getattr(cs, "restart_count", 0) or 0) >= (
            MOUNT_SETUP_RESTARTS
        ):
            return True
    return False


def _list_probe_pods(now: float) -> Dict[tuple[str, str], Probe] | None:
    """{(mount_key, node_key): Probe} for every probe DaemonSet pod.

    The node comes from spec.nodeName: one DaemonSet template covers every
    node, so no label can carry it.

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

    probes: Dict[tuple[str, str], Probe] = {}
    failed: Dict[tuple[str, str], bool] = {}
    for pod in pods.items:
        labels = getattr(pod.metadata, "labels", None) or {}
        mount_key = labels.get("mount", "")
        node_key = _sanitized_node_name(getattr(pod.spec, "node_name", "") or "")
        if not mount_key or not node_key:
            continue
        key = (mount_key, node_key)
        probe = probes.setdefault(key, Probe())
        ready = _probe_pod_ready(pod)
        status = getattr(pod, "status", None)
        ip = getattr(status, "pod_ip", None) or ""
        terminating = bool(getattr(pod.metadata, "deletion_timestamp", None))
        # Two pods for one node exist briefly during a rollout; Ready wins.
        if ip and not terminating and (ready or not probe.ip):
            probe.ip = ip
            probe.port = _probe_port(pod)
        probe.ready = probe.ready or ready
        failed[key] = failed.get(key, False) or _mount_setup_failed(pod, now)
    for key, probe in probes.items():
        probe.setup_failed = failed[key] and not probe.ready
    return probes


_probe_cache: Dict[tuple[str, str], Probe] | None = None
_probe_cache_ts: float = 0.0


def _probe_pods(now: float) -> Dict[tuple[str, str], Probe] | None:
    global _probe_cache, _probe_cache_ts
    probes = _list_probe_pods(now)
    if probes is not None:
        _probe_cache, _probe_cache_ts = probes, now
        return probes
    if _probe_cache is not None and now - _probe_cache_ts < POD_CACHE_TTL_S:
        return _probe_cache
    return None


# No proxy: a pod IP is never reached through one.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_executor = ThreadPoolExecutor(max_workers=FETCH_WORKERS)


def _fetch_result(ip: str, port: int) -> Dict[str, Any] | None:
    host = f"[{ip}]" if ":" in ip else ip
    try:
        with _opener.open(
            f"http://{host}:{port}/result", timeout=FETCH_TIMEOUT_S
        ) as resp:
            data = json.loads(resp.read(MAX_RESULT_BYTES))
    except Exception as e:
        _vlog(f"[node_healthcheck] No result from {host}:{port}: {e}")
        return None
    return data if isinstance(data, dict) else None


def _fetch_all(
    targets: Dict[tuple[str, str], Probe],
) -> Dict[tuple[str, str], Dict[str, Any]]:
    futures = {
        _executor.submit(_fetch_result, probe.ip, probe.port): key
        for key, probe in targets.items()
    }
    done, _pending = wait(futures, timeout=FETCH_PASS_TIMEOUT_S)
    results: Dict[tuple[str, str], Dict[str, Any]] = {}
    for future in done:
        value = future.result()
        if value is not None:
            results[futures[future]] = value
    return results


# Last result per probe: a missed pull keeps its verdict, under the staleness rules.
_result_cache: Dict[tuple[str, str], Dict[str, Any]] = {}


def update_metrics() -> None:
    now = time.time()

    af_nodes = _list_af_nodes()
    probes = _probe_pods(now)

    ready_nodes = {_sanitized_node_name(name) for name, _pool, ok in af_nodes if ok}
    targets = {
        key: probe
        for key, probe in (probes or {}).items()
        if probe.ip and key[1] in ready_nodes
    }
    fetched = _fetch_all(targets)
    _result_cache.update(fetched)

    expected = [key for key, probe in targets.items() if probe.ready]
    answered = [key for key in expected if key in fetched]
    # A probe answering proves the node; a stuck sibling there is down to its volume.
    working_nodes = {node for _mount, node in answered}

    for m_name, mount_path in MOUNTS.items():
        mount_key = _sanitized_mount_name(m_name)
        for node_name, pool, ready in af_nodes:
            labels = {
                "mount_name": m_name,
                "mount_path": mount_path,
                "node": node_name,
                "node_pool": pool,
            }

            # NotReady: probes cannot report. Clear gauges so Prometheus/Grafana
            # see null (gap), not last-known-good green and not a false red. Red
            # is reserved for a completed failing check on a Ready node
            # (fresh=1).
            if not ready:
                _clear_mount_gauges(labels)
                continue

            node_key = _sanitized_node_name(node_name)
            key = (mount_key, node_key)
            probe = probes.get(key) if probes is not None else None
            probe_up = (
                bool(probe and probe.ready and key in fetched)
                if probes is not None
                else None
            )

            if probe is not None and probe.setup_failed and node_key in working_nodes:
                _publish_probe_up(labels, probe_up)
                _publish_setup_failed(labels)
                continue

            data = _result_cache.get(key)
            # Use node from result JSON (the probe pod's node); fallback to
            # discovery so the metric always reflects the node that produced it.
            node_for_label = (
                (data.get("node") or "").strip() if data else ""
            ) or node_name

            labels = {
                "mount_name": m_name,
                "mount_path": mount_path,
                "node": node_for_label,
                "node_pool": _node_pools.get(node_for_label, pool),
            }
            _publish_probe_up(labels, probe_up)

            if not data:
                # No result yet: expose timeout semantics for latency/throughput
                # gauges so alerts/dashboards see an explicit failure signal.
                if probe is None:
                    check_type = "no_recent_result"
                elif probe.ready:
                    # Ready by the kubelet's check, but not reachable from here.
                    check_type = "unreachable"
                else:
                    # Pending, or crashing before its first result.
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
                elif ok:
                    # Not measured yet: absent, not a zero rate left by an earlier sentinel.
                    _clear_gauges(labels, (mount_data_rate_gbps,))

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

    if expected:
        monitor_results_available.set(len(answered) / len(expected))
    else:
        # No Ready probe to ask: nothing is being checked on any Ready node.
        monitor_results_available.set(0 if ready_nodes else 1)


def main() -> None:
    """Export until kubelet swaps other code than this into the mounted ConfigMap."""
    running = CODE.read_bytes()
    start_http_server(8000)
    while CODE.read_bytes() == running:
        try:
            update_metrics()
            monitor_last_iteration_ts.set(time.time())
        except Exception as e:
            _elog(f"[node_healthcheck] update_metrics failed: {e}")
        time.sleep(CHECK_INTERVAL_S)
    print(
        f"[node_healthcheck] {CODE} changed: exiting, for the container to restart on it"
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
