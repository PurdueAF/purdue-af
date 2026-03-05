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
    "JOB_IMAGE", "geddes-registry.rcac.purdue.edu/cms/af-node-monitor:0.1.0"
)

NODE_CACHE_TTL_S = float(os.getenv("NODE_CACHE_TTL_S", "300"))


try:
    mount_valid = Gauge(
        "af_node_mount_valid",
        "Storage mount health",
        ["mount_name", "mount_path", "node"],
    )
    mount_ping_ms = Gauge(
        "af_node_mount_ping_ms",
        "Storage mount ping time in milliseconds",
        ["mount_name", "mount_path", "node"],
    )
    mount_data_rate_gbps = Gauge(
        "af_node_mount_data_rate_gbps",
        "Storage mount sequential read throughput in Gbps",
        ["mount_name", "mount_path", "node"],
    )
    mount_metadata_latency_ms = Gauge(
        "af_node_mount_metadata_latency_ms",
        "Storage mount metadata latency in milliseconds (ls)",
        ["mount_name", "mount_path", "node"],
    )

    mount_timeout_total = Counter(
        "af_node_mount_timeout_total",
        "Total number of timeouts contacting mount workers or running checks",
        ["mount_name", "mount_path", "node", "check_type"],
    )
    mount_last_success_ts = Gauge(
        "af_node_mount_last_success_timestamp_seconds",
        "Unix timestamp of last successful metrics update for mount",
        ["mount_name", "mount_path", "node"],
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

_node_cache: List[str] = []
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
        print("[node_healthcheck] Kubernetes client initialized")
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
            return json.load(f)
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


def _list_target_nodes() -> List[str]:
    """Return a cached list of Ready AF node names based on labels."""
    _init_k8s()
    if not _k8s_ready or _core_v1 is None:
        return []

    global _node_cache, _last_node_refresh
    now = time.time()
    if _node_cache and (now - _last_node_refresh) < NODE_CACHE_TTL_S:
        return _node_cache

    names: set[str] = set()
    label_sets = [
        "cms-af-prod=true",
        "cms-af-dev=true",
    ]
    try:
        for selector in label_sets:
            resp = _core_v1.list_node(label_selector=selector)
            for node in resp.items:
                if not node.metadata or not node.metadata.name:
                    continue
                conditions = getattr(node.status, "conditions", None)
                if not conditions:
                    continue
                ready = False
                for cond in conditions:
                    if (
                        getattr(cond, "type", "") == "Ready"
                        and getattr(cond, "status", "") == "True"
                    ):
                        ready = True
                        break
                if ready:
                    names.add(node.metadata.name)
    except ApiException as e:  # type: ignore[misc]
        print(f"[node_healthcheck] Error listing nodes: {e}")
    except Exception as e:  # pragma: no cover - defensive
        print(f"[node_healthcheck] Unexpected error listing nodes: {e}")

    _node_cache = sorted(names)
    _last_node_refresh = now
    return _node_cache


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
                print(
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
                    print(
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
            print(
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

    nodes = _list_target_nodes()
    if not nodes:
        # Fallback: still try to read legacy per-mount results.
        nodes = [""]

    for m_name, cfg in MOUNTS.items():
        mount_path = cfg["mount_path"]
        for node_name in nodes:
            data = _load_result(m_name, node_name)

            labels = {
                "mount_name": m_name,
                "mount_path": mount_path,
                "node": node_name or "unknown",
            }

            if data and data.get("_storage_error"):
                # Results PVC is unavailable; skip metrics entirely so they appear empty.
                continue

            if not data:
                # No result yet - treat as timeout/error with last metrics preserved
                mount_valid.labels(**labels).set(0)
                mount_timeout_total.labels(
                    check_type="no_recent_result", **labels
                ).inc()
                continue

            timestamp = float(data.get("timestamp", 0))
            # Consider stale if older than configured stale window
            if now - timestamp > RESULT_STALE_WINDOW_S:
                mount_valid.labels(**labels).set(0)
                mount_timeout_total.labels(check_type="stale_result", **labels).inc()
                continue

            timeout = bool(data.get("timeout", False))
            ok = bool(data.get("ok", False)) and not timeout

            ping_ms = data.get("ping_ms")
            meta_ms = data.get("metadata_ms")
            gbps = data.get("throughput_gbps")

            mount_valid.labels(**labels).set(1 if ok else 0)

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

                mount_last_success_ts.labels(**labels).set(timestamp)


if __name__ == "__main__":
    start_http_server(8000)
    while True:
        update_metrics()
        monitor_last_iteration_ts.set(time.time())
        time.sleep(CHECK_INTERVAL_S)
