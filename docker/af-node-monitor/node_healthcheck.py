# Lazy annotations: required for the `client = None` fallback below — without
# this, module-level annotations like `client.CoreV1Api | None` are evaluated
# at import time and crash when kubernetes is not installed (local runs/tests).
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
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

MOUNTS: Dict[str, Dict[str, Any]] = {
    "/depot/": {
        "mount_path": "/depot/",
        "job": {
            "check_file": "/depot/cms/purdue-af/validate-mount.txt",
            "checksum": "13dede34ee8dc7e5b70c9cd06ac15467",
            "metadata_dir": "/depot/cms/",
            "fio_file": "/depot/cms/purdue-af/.storage-monitoring-probe-1gb",
            "enable_fio": True,
        },
        "volumes": [
            {
                "name": "results",
                "persistentVolumeClaim": {"claimName": "af-node-monitor-storage"},
            },
            {
                "name": "depot",
                "nfs": {
                    "server": "datadepot.rcac.purdue.edu",
                    "path": "/depot/cms",
                },
            },
        ],
        "volume_mounts": [
            {"name": "results", "mountPath": "/af-node-monitor"},
            {
                "name": "depot",
                "mountPath": "/depot/cms",
                "mountPropagation": "HostToContainer",
            },
        ],
    },
    "/work/": {
        "mount_path": "/work/",
        "job": {
            "check_file": "/work/projects/purdue-af/validate-mount.txt",
            "checksum": "f4cb7f2740ba3e87edfbda6c70fa94c2",
            "metadata_dir": "/work/users/",
            "fio_file": "/work/projects/purdue-af/.storage-monitoring-probe-1gb",
            "enable_fio": True,
        },
        "volumes": [
            {
                "name": "results",
                "persistentVolumeClaim": {"claimName": "af-node-monitor-storage"},
            },
            {
                "name": "work",
                "persistentVolumeClaim": {"claimName": "af-shared-storage"},
            },
        ],
        "volume_mounts": [
            {"name": "results", "mountPath": "/af-node-monitor"},
            {"name": "work", "mountPath": "/work"},
        ],
    },
    "eos": {
        "mount_path": "eos",
        "job": {
            "check_file": "/eos/purdue/store/user/dkondrat/test.root",
            "checksum": "18864b0de8ae5a6a8d3b459a7999b431",
            "metadata_dir": "/eos/purdue/store/user/",
            "fio_file": "/eos/purdue/store/user/dkondrat/.storage-monitoring-probe-1gb",
            "enable_fio": True,
        },
        "volumes": [
            {
                "name": "results",
                "persistentVolumeClaim": {"claimName": "af-node-monitor-storage"},
            },
            {"name": "eos", "hostPath": {"path": "/eos"}},
        ],
        "volume_mounts": [
            {"name": "results", "mountPath": "/af-node-monitor"},
            {
                "name": "eos",
                "mountPath": "/eos",
                "mountPropagation": "HostToContainer",
            },
        ],
    },
    "cvmfs": {
        "mount_path": "cvmfs",
        "job": {
            "check_file": "/cvmfs/cms.cern.ch/SITECONF/T2_US_Purdue/Purdue-Hadoop/JobConfig/site-local-config.xml",
            "checksum": "3b570d80272b7188c13cef51e58b7151",
            "metadata_dir": "/cvmfs/cms.cern.ch/",
            "enable_fio": False,
        },
        "volumes": [
            {
                "name": "results",
                "persistentVolumeClaim": {"claimName": "af-node-monitor-storage"},
            },
            {"name": "cvmfs", "persistentVolumeClaim": {"claimName": "cvmfs"}},
        ],
        "volume_mounts": [
            {"name": "results", "mountPath": "/af-node-monitor"},
            {
                "name": "cvmfs",
                "mountPath": "/cvmfs",
                "mountPropagation": "HostToContainer",
            },
        ],
    },
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


JOB_INTERVAL_S = float(os.getenv("JOB_INTERVAL_S", "600"))  # 10 minutes
JOB_TTL_SECONDS = int(
    os.getenv("JOB_TTL_SECONDS", "120")
)  # ttlSecondsAfterFinished for Jobs
JOB_ACTIVE_DEADLINE_SECONDS = int(os.getenv("JOB_ACTIVE_DEADLINE_SECONDS", "180"))
JOB_BACKOFF_LIMIT = int(os.getenv("JOB_BACKOFF_LIMIT", "0"))

JOB_SUCCESS_RETENTION_S = float(
    os.getenv("JOB_SUCCESS_RETENTION_S", "0")
)  # delete successful Jobs immediately
JOB_FAILED_RETENTION_S = float(
    os.getenv("JOB_FAILED_RETENTION_S", "60")
)  # keep failed Jobs for 1 minute
JOB_MAX_RUNTIME_S = float(os.getenv("JOB_MAX_RUNTIME_S", "300"))  # 5 minutes

RESULT_STALE_WINDOW_S = float(
    os.getenv("RESULT_STALE_WINDOW_S", str(3 * JOB_INTERVAL_S))
)

JOB_IMAGE = os.getenv(
    "JOB_IMAGE",
    "geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/af-node-monitor:latest",
)

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
    monitor_last_iteration_ts = Gauge(
        "af_node_monitor_last_iteration_timestamp_seconds",
        "Unix timestamp of last completed metrics iteration",
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

# Ready nodes only — Jobs are pinned to these.
_node_cache: List[str] = []
# All AF-labelled nodes as (name, pool, ready). Metrics must cover NotReady
# nodes too: otherwise last-known-good gauges freeze green while jobs cannot run.
_af_nodes_cache: List[tuple[str, str, bool]] = []
_last_node_refresh: float = 0.0

_last_job_start_ts: dict[str, dict[str, float]] = defaultdict(dict)


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


def _load_result(mount_name: str, node_name: str) -> Dict[str, Any] | None:
    path = _result_path(mount_name, node_name)
    try:
        with path.open("r", encoding="utf-8") as f:
            loaded: Dict[str, Any] = json.load(f)
            return loaded
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

    global _node_cache, _af_nodes_cache, _last_node_refresh
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
    _node_cache = [name for name, _pool, ready in _af_nodes_cache if ready]
    _last_node_refresh = now


def _list_target_nodes() -> List[str]:
    """Return Ready AF node names — Jobs are only scheduled on these."""
    _refresh_node_caches()
    if not _k8s_ready or _core_v1 is None:
        return []
    return _node_cache


def _list_af_nodes() -> List[tuple[str, str, bool]]:
    """Return all AF-labelled nodes as (name, pool, ready).

    NotReady nodes are included so their last-known-good gauges can be cleared
    (null in Prometheus) rather than freezing green while Jobs cannot run.
    """
    _refresh_node_caches()
    if not _k8s_ready or _core_v1 is None:
        return []
    return _af_nodes_cache


def _clear_mount_gauges(labels: dict[str, str]) -> None:
    """Drop status gauges for a mount/node so scrapes show null, not stale values.

    Counters are left alone — they are cumulative and do not drive green/red.
    """
    labelvalues = (
        labels["mount_name"],
        labels["mount_path"],
        labels["node"],
        labels["node_pool"],
    )
    for gauge in (
        mount_valid,
        mount_ping_ms,
        mount_data_rate_gbps,
        mount_metadata_latency_ms,
        mount_result_fresh,
        mount_last_success_ts,
    ):
        try:
            gauge.remove(*labelvalues)
        except KeyError:
            pass


def _clear_node_pool_gauges(node_name: str, pool: str) -> None:
    """Drop all mount status gauges for a node under one node_pool label.

    prometheus_client keeps every label set forever until remove(). When a
    node flips between cms-af-dev and cms-af-prod (or leaves the AF set), the
    inactive pool's series must be dropped — otherwise scrapes keep exporting
    a frozen timeout sentinel that Grafana heatmaps can sum into a false red.
    """
    for m_name, cfg in MOUNTS.items():
        _clear_mount_gauges(
            {
                "mount_name": m_name,
                "mount_path": cfg["mount_path"],
                "node": node_name,
                "node_pool": pool,
            }
        )


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


def _has_active_job(mount_name: str, node_name: str) -> bool:
    _init_k8s()
    if not _k8s_ready or _batch_v1 is None:
        return False

    mount_key = _sanitized_mount_name(mount_name)
    label_selector = (
        f"app=af-node-monitor,mount={mount_key},node={_sanitized_node_name(node_name)}"
    )
    try:
        jobs = _batch_v1.list_namespaced_job(
            namespace=POD_NAMESPACE, label_selector=label_selector
        )
    except ApiException as e:  # type: ignore[misc]
        print(f"[node_healthcheck] Error listing Jobs: {e}")
        return False
    except Exception as e:  # pragma: no cover - defensive
        print(f"[node_healthcheck] Unexpected error listing Jobs: {e}")
        return False

    for job in jobs.items:
        status = job.status
        if status and getattr(status, "active", 0):
            return True
    return False


def _list_active_job_keys() -> set[tuple[str, str]]:
    """Return {(mount_name, node_name)} keys that currently have active Jobs."""
    _init_k8s()
    if not _k8s_ready or _batch_v1 is None:
        return set()

    keys: set[tuple[str, str]] = set()
    try:
        jobs = _batch_v1.list_namespaced_job(
            namespace=POD_NAMESPACE, label_selector="app=af-node-monitor"
        )
    except ApiException as e:  # type: ignore[misc]
        _elog(f"[node_healthcheck] Error listing Jobs for active index: {e}")
        return set()
    except Exception as e:  # pragma: no cover - defensive
        _elog(f"[node_healthcheck] Unexpected error listing Jobs for active index: {e}")
        return set()

    for job in jobs.items:
        status = job.status
        if not status or not getattr(status, "active", 0):
            continue
        labels = getattr(job.metadata, "labels", None) or {}
        mount_key = labels.get("mount", "")
        node_key = labels.get("node", "")
        if not mount_key or not node_key:
            continue
        keys.add((mount_key, node_key))
    return keys


def _mount_job_env(mount_name: str, cfg: Dict[str, Any]) -> list[dict[str, Any]]:
    env_cfg = cfg.get("job", {})
    env: list[dict[str, Any]] = [
        {"name": "MOUNT_NAME", "value": mount_name},
    ]
    if env_cfg.get("check_file"):
        env.append({"name": "CHECK_FILE", "value": env_cfg["check_file"]})
    if env_cfg.get("checksum"):
        env.append({"name": "CHECKSUM", "value": env_cfg["checksum"]})
    if env_cfg.get("metadata_dir"):
        env.append({"name": "METADATA_DIR", "value": env_cfg["metadata_dir"]})
    if env_cfg.get("fio_file"):
        env.append({"name": "FIO_FILE", "value": env_cfg["fio_file"]})
    if env_cfg.get("enable_fio") is not None:
        env.append({"name": "ENABLE_FIO", "value": str(env_cfg["enable_fio"]).lower()})

    # Timeouts and intervals.
    env.extend(
        [
            {"name": "PING_TIMEOUT_S", "value": str(PING_TIMEOUT_S)},
            {"name": "METADATA_TIMEOUT_S", "value": str(METADATA_TIMEOUT_S)},
            {"name": "FIO_TIMEOUT_S", "value": str(FIO_TIMEOUT_S)},
            {
                "name": "FIO_INTERVAL_S",
                "value": str(env_cfg.get("fio_interval_s", 1800)),
            },
            {"name": "RESULTS_DIR", "value": str(RESULTS_DIR)},
            {
                "name": "AF_NODE_MONITOR_VERBOSE",
                "value": os.getenv("AF_NODE_MONITOR_VERBOSE", "false"),
            },
            {
                "name": "NODE_NAME",
                "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}},
            },
        ]
    )
    return env


DEFAULT_TOLERATIONS: list[dict[str, Any]] = [
    {
        "key": "hub.jupyter.org/dedicated",
        "operator": "Equal",
        "value": "cms-af",
        "effect": "NoSchedule",
    }
]

DEFAULT_AFFINITY: dict[str, Any] = {
    "nodeAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [
                {
                    "matchExpressions": [
                        {
                            "key": "cms-af-prod",
                            "operator": "In",
                            "values": ["true"],
                        }
                    ]
                },
                {
                    "matchExpressions": [
                        {
                            "key": "cms-af-dev",
                            "operator": "In",
                            "values": ["true"],
                        }
                    ]
                },
            ]
        }
    }
}


def _build_job_manifest(
    mount_name: str, cfg: Dict[str, Any], node_name: str
) -> Dict[str, Any]:
    mount_key = _sanitized_mount_name(mount_name)
    node_key = _sanitized_node_name(node_name)
    ts = int(time.time())
    job_name = f"af-node-monitor-{mount_key}-{node_key}-{ts}"

    labels = {
        "app": "af-node-monitor",
        "mount": mount_key,
        "node": node_key,
    }

    volumes = cfg.get("volumes", [])
    volume_mounts = cfg.get("volume_mounts", [])

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "labels": labels,
        },
        "spec": {
            "ttlSecondsAfterFinished": JOB_TTL_SECONDS,
            "backoffLimit": JOB_BACKOFF_LIMIT,
            "activeDeadlineSeconds": JOB_ACTIVE_DEADLINE_SECONDS,
            "template": {
                "metadata": {
                    "labels": labels,
                },
                "spec": {
                    "restartPolicy": "Never",
                    "enableServiceLinks": False,
                    "nodeName": node_name,
                    "affinity": DEFAULT_AFFINITY,
                    "tolerations": DEFAULT_TOLERATIONS,
                    "containers": [
                        {
                            "name": "af-node-monitor-job",
                            "image": JOB_IMAGE,
                            "command": ["python", "/opt/af-node-monitor/job_runner.py"],
                            "env": _mount_job_env(mount_name, cfg),
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "64Mi"},
                                "limits": {"cpu": "200m", "memory": "256Mi"},
                            },
                            "volumeMounts": volume_mounts,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def _ensure_jobs(now: float) -> None:
    _init_k8s()
    if not _k8s_ready or _batch_v1 is None:
        return

    nodes = _list_target_nodes()
    if not nodes:
        return

    for mount_name, cfg in MOUNTS.items():
        for node_name in nodes:
            last_ts = _last_job_start_ts[mount_name].get(node_name, 0.0)
            if now - last_ts < JOB_INTERVAL_S:
                continue
            if _has_active_job(mount_name, node_name):
                continue

            body = _build_job_manifest(mount_name, cfg, node_name)
            try:
                _batch_v1.create_namespaced_job(namespace=POD_NAMESPACE, body=body)
                _last_job_start_ts[mount_name][node_name] = now
                _vlog(
                    "[node_healthcheck] Created Job "
                    f"{body['metadata']['name']} for mount='{mount_name}' node='{node_name}'"
                )
            except ApiException as e:  # type: ignore[misc]
                print(
                    f"[node_healthcheck] Failed to create Job for mount='{mount_name}' "
                    f"node='{node_name}': {e}"
                )
            except Exception as e:  # pragma: no cover - defensive
                print(
                    f"[node_healthcheck] Unexpected error creating Job for mount='{mount_name}' "
                    f"node='{node_name}': {e}"
                )


def _cleanup_finished_jobs(now: float) -> None:
    _init_k8s()
    if not _k8s_ready or _batch_v1 is None:
        return

    try:
        jobs = _batch_v1.list_namespaced_job(
            namespace=POD_NAMESPACE, label_selector="app=af-node-monitor"
        )
    except ApiException as e:  # type: ignore[misc]
        print(f"[node_healthcheck] Error listing Jobs for cleanup: {e}")
        return
    except Exception as e:  # pragma: no cover - defensive
        print(f"[node_healthcheck] Unexpected error listing Jobs for cleanup: {e}")
        return

    for job in jobs.items:
        status = job.status
        metadata = job.metadata
        if not status or not metadata or not metadata.name:
            continue

        start_time = getattr(status, "start_time", None)
        active = getattr(status, "active", 0) or 0

        # Force-kill long-running Jobs.
        if active and start_time is not None:
            runtime = now - start_time.timestamp()
            if runtime > JOB_MAX_RUNTIME_S:
                try:
                    _batch_v1.delete_namespaced_job(
                        name=metadata.name,
                        namespace=POD_NAMESPACE,
                        propagation_policy="Background",
                        body=client.V1DeleteOptions(grace_period_seconds=0),
                    )
                    _vlog(
                        f"[node_healthcheck] Force-deleted long-running Job "
                        f"{metadata.name} after {int(runtime)}s"
                    )
                except ApiException as e:  # type: ignore[misc]
                    print(
                        f"[node_healthcheck] Failed to force-delete Job {metadata.name}: {e}"
                    )
                except Exception as e:  # pragma: no cover - defensive
                    print(
                        f"[node_healthcheck] Unexpected error force-deleting Job {metadata.name}: {e}"
                    )
                continue

        if active:
            # Still running but within allowed runtime.
            continue

        completion_time = getattr(status, "completion_time", None)
        if completion_time is None:
            completion_time = getattr(status, "start_time", None)
        if completion_time is None:
            continue

        finished_ago = now - completion_time.timestamp()
        # Classify Job outcome.
        succeeded = bool(getattr(status, "succeeded", 0))
        failed = bool(getattr(status, "failed", 0))
        for cond in getattr(status, "conditions", []) or []:
            ctype = getattr(cond, "type", "")
            cstatus = getattr(cond, "status", "")
            if ctype == "Complete" and cstatus == "True":
                succeeded = True
            if ctype == "Failed" and cstatus == "True":
                failed = True

        if succeeded and not failed:
            # Successful Jobs: delete immediately (or after optional small delay).
            if finished_ago < JOB_SUCCESS_RETENTION_S:
                continue
        else:
            # Failed or unknown outcome: keep briefly for inspection.
            if finished_ago < JOB_FAILED_RETENTION_S:
                continue

        try:
            _batch_v1.delete_namespaced_job(
                name=metadata.name,
                namespace=POD_NAMESPACE,
                propagation_policy="Background",
            )
            _vlog(
                f"[node_healthcheck] Deleted finished Job {metadata.name} "
                f"after {int(finished_ago)}s"
            )
        except ApiException as e:  # type: ignore[misc]
            print(f"[node_healthcheck] Failed to delete Job {metadata.name}: {e}")
        except Exception as e:  # pragma: no cover - defensive
            print(
                f"[node_healthcheck] Unexpected error deleting Job {metadata.name}: {e}"
            )


def update_metrics() -> None:
    now = time.time()

    # Ensure per-mount per-node Jobs are running.
    _ensure_jobs(now)

    # Explicitly clean up finished Jobs after a short retention.
    _cleanup_finished_jobs(now)

    af_nodes = _list_af_nodes()
    if not af_nodes:
        # Fallback: still try to read legacy per-mount results.
        af_nodes = [("", "prod", True)]
    active_job_keys = _list_active_job_keys()

    for m_name, cfg in MOUNTS.items():
        mount_path = cfg["mount_path"]
        for node_name, pool, ready in af_nodes:
            labels = {
                "mount_name": m_name,
                "mount_path": mount_path,
                "node": node_name or "unknown",
                "node_pool": pool,
            }

            # NotReady: Jobs cannot run. Clear gauges so Prometheus/Grafana see
            # null (gap), not last-known-good green and not a false red. Red is
            # reserved for a completed failing check on a Ready node (fresh=1).
            if node_name and not ready:
                labels["node"] = node_name
                _clear_mount_gauges(labels)
                continue

            data = _load_result(m_name, node_name)
            # Use node from result JSON (job pod's node); fallback to discovery
            # so the metric always reflects the node that produced the data.
            node_for_label = ((data.get("node") or "").strip() if data else "") or (
                node_name or "unknown"
            )

            labels = {
                "mount_name": m_name,
                "mount_path": mount_path,
                "node": node_for_label,
                "node_pool": _node_pools.get(node_for_label, pool),
            }

            if data and data.get("_storage_error"):
                # Results PVC is unavailable; drop series so they appear empty.
                _clear_mount_gauges(labels)
                continue

            if not data:
                # No result yet: expose timeout semantics for latency/throughput gauges
                # so alerts/dashboards see an explicit failure signal.
                check_type = "no_recent_result"
                mount_key = _sanitized_mount_name(m_name)
                node_key = _sanitized_node_name(node_name)
                if node_key and (mount_key, node_key) in active_job_keys:
                    # Distinguish the case where a Job exists but has not produced output
                    # yet (for example stuck Pending/ContainerCreating).
                    check_type = "job_never_started"
                _publish_unusable(labels, check_type)
                continue

            timestamp = float(data.get("timestamp", 0))
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


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    start_http_server(8000)
    while True:
        try:
            update_metrics()
            monitor_last_iteration_ts.set(time.time())
        except Exception as e:
            _elog(f"[node_healthcheck] update_metrics failed: {e}")
        time.sleep(CHECK_INTERVAL_S)
