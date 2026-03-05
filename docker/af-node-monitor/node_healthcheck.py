import json
import os
import time
from pathlib import Path
from typing import Any, Dict

from prometheus_client import Counter, Gauge, start_http_server

MOUNTS: Dict[str, Dict[str, Any]] = {
    "/depot/": {
        "mount_path": "/depot/",
    },
    "/work/": {
        "mount_path": "/work/",
    },
    "eos": {
        "mount_path": "eos",
    },
    "cvmfs": {
        "mount_path": "cvmfs",
    },
}

PING_TIMEOUT_S = float(os.getenv("PING_TIMEOUT_S", "3"))
METADATA_TIMEOUT_S = float(os.getenv("METADATA_TIMEOUT_S", "10"))
FIO_TIMEOUT_S = float(os.getenv("FIO_TIMEOUT_S", "120"))

CHECK_INTERVAL_S = float(os.getenv("CHECK_INTERVAL_S", "120"))
RESULTS_DIR = Path(os.getenv("RESULTS_DIR", "/work/af-node-monitor/results"))


try:
    mount_valid = Gauge(
        "af_node_mount_valid", "Storage mount health", ["mount_name", "mount_path"]
    )
    mount_ping_ms = Gauge(
        "af_node_mount_ping_ms",
        "Storage mount ping time in milliseconds",
        ["mount_name", "mount_path"],
    )
    mount_data_rate_gbps = Gauge(
        "af_node_mount_data_rate_gbps",
        "Storage mount sequential read throughput in Gbps",
        ["mount_name", "mount_path"],
    )
    mount_metadata_latency_ms = Gauge(
        "af_node_mount_metadata_latency_ms",
        "Storage mount metadata latency in milliseconds (ls)",
        ["mount_name", "mount_path"],
    )

    mount_timeout_total = Counter(
        "af_node_mount_timeout_total",
        "Total number of timeouts contacting mount workers or running checks",
        ["mount_name", "mount_path", "check_type"],
    )
    mount_error_total = Counter(
        "af_node_mount_error_total",
        "Total number of errors contacting mount workers or running checks",
        ["mount_name", "mount_path", "check_type"],
    )
    mount_last_success_ts = Gauge(
        "af_node_mount_last_success_timestamp_seconds",
        "Unix timestamp of last successful metrics update for mount",
        ["mount_name", "mount_path"],
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


def _result_path(mount_name: str) -> Path:
    key = mount_name.strip("/").replace("/", "_") or "root"
    return RESULTS_DIR / f"{key}.json"


def _load_result(mount_name: str) -> Dict[str, Any] | None:
    path = _result_path(mount_name)
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"Error reading result for {mount_name} from {path}: {e}")
        return None


def update_metrics() -> None:
    now = time.time()

    for m_name, cfg in MOUNTS.items():
        mount_path = cfg["mount_path"]
        data = _load_result(m_name)

        if not data:
            # No result yet - treat as timeout/error with last metrics preserved
            mount_valid.labels(mount_name=m_name, mount_path=mount_path).set(0)
            mount_timeout_total.labels(
                mount_name=m_name, mount_path=mount_path, check_type="no_recent_result"
            ).inc()
            continue

        timestamp = float(data.get("timestamp", 0))
        # Consider stale if older than 3 * CHECK_INTERVAL_S
        if now - timestamp > 3 * CHECK_INTERVAL_S:
            mount_valid.labels(mount_name=m_name, mount_path=mount_path).set(0)
            mount_timeout_total.labels(
                mount_name=m_name, mount_path=mount_path, check_type="stale_result"
            ).inc()
            continue

        timeout = bool(data.get("timeout", False))
        ok = bool(data.get("ok", False)) and not timeout

        ping_ms = data.get("ping_ms")
        meta_ms = data.get("metadata_ms")
        gbps = data.get("throughput_gbps")

        mount_valid.labels(mount_name=m_name, mount_path=mount_path).set(1 if ok else 0)

        if timeout:
            # Values in JSON already represent timeout semantics; enforce speed 0.
            if ping_ms is not None:
                mount_ping_ms.labels(mount_name=m_name, mount_path=mount_path).set(
                    float(ping_ms)
                )
            else:
                mount_ping_ms.labels(mount_name=m_name, mount_path=mount_path).set(
                    _timeout_ping_ms()
                )

            if meta_ms is not None:
                mount_metadata_latency_ms.labels(
                    mount_name=m_name, mount_path=mount_path
                ).set(float(meta_ms))
            else:
                mount_metadata_latency_ms.labels(
                    mount_name=m_name, mount_path=mount_path
                ).set(_timeout_metadata_ms())

            mount_data_rate_gbps.labels(mount_name=m_name, mount_path=mount_path).set(
                0.0
            )
            mount_timeout_total.labels(
                mount_name=m_name, mount_path=mount_path, check_type="job_result"
            ).inc()
        else:
            if ping_ms is not None:
                mount_ping_ms.labels(mount_name=m_name, mount_path=mount_path).set(
                    float(ping_ms)
                )

            if meta_ms is not None:
                mount_metadata_latency_ms.labels(
                    mount_name=m_name, mount_path=mount_path
                ).set(float(meta_ms))

            if gbps is not None:
                mount_data_rate_gbps.labels(
                    mount_name=m_name, mount_path=mount_path
                ).set(float(gbps))

            mount_last_success_ts.labels(mount_name=m_name, mount_path=mount_path).set(
                timestamp
            )


if __name__ == "__main__":
    start_http_server(8000)
    while True:
        update_metrics()
        monitor_last_iteration_ts.set(time.time())
        time.sleep(CHECK_INTERVAL_S)
