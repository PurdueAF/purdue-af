import os
import time
from typing import Any, Dict

import requests
from prometheus_client import Gauge, start_http_server

MOUNTS: Dict[str, Dict[str, Any]] = {
    # mount_name: {mount_path label, worker_url}
    "/depot/": {
        "mount_path": "/depot/",
        "worker_url": os.getenv(
            "DEPOT_WORKER_URL", "http://af-node-monitor-depot-worker:8080/check"
        ),
    },
    "/work/": {
        "mount_path": "/work/",
        "worker_url": os.getenv(
            "WORK_WORKER_URL", "http://af-node-monitor-work-worker:8080/check"
        ),
    },
    "eos": {
        "mount_path": "eos",
        "worker_url": os.getenv(
            "EOS_WORKER_URL", "http://af-node-monitor-eos-worker:8080/check"
        ),
    },
    "cvmfs": {
        "mount_path": "cvmfs",
        "worker_url": os.getenv(
            "CVMFS_WORKER_URL", "http://af-node-monitor-cvmfs-worker:8080/check"
        ),
    },
}

PING_TIMEOUT_S = float(os.getenv("PING_TIMEOUT_S", "3"))
METADATA_TIMEOUT_S = float(os.getenv("METADATA_TIMEOUT_S", "10"))
FIO_TIMEOUT_S = float(os.getenv("FIO_TIMEOUT_S", "120"))

HTTP_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "15"))
CHECK_INTERVAL_S = float(os.getenv("CHECK_INTERVAL_S", "120"))


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
except Exception as e:  # pragma: no cover - defensive
    print(f"Error defining Gauge metric: {e}")


def _timeout_ping_ms() -> float:
    return PING_TIMEOUT_S * 1000.0


def _timeout_metadata_ms() -> float:
    return METADATA_TIMEOUT_S * 1000.0


def _call_worker(url: str) -> Dict[str, Any] | None:
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT_S)
        if resp.status_code != 200:
            print(f"Worker {url} returned status {resp.status_code}")
            return None
        return resp.json()
    except requests.Timeout:
        print(f"Worker {url} timed out after {HTTP_TIMEOUT_S}s")
        return {"timeout": True}
    except Exception as e:
        print(f"Worker {url} error: {e}")
        return None


def update_metrics() -> None:
    for m_name, cfg in MOUNTS.items():
        mount_path = cfg["mount_path"]
        url = cfg["worker_url"]

        data = _call_worker(url)

        if data is None:
            # Treat as error: mark invalid, set timeout-like values
            mount_valid.labels(mount_name=m_name, mount_path=mount_path).set(0)
            mount_ping_ms.labels(mount_name=m_name, mount_path=mount_path).set(
                _timeout_ping_ms()
            )
            mount_metadata_latency_ms.labels(
                mount_name=m_name, mount_path=mount_path
            ).set(_timeout_metadata_ms())
            mount_data_rate_gbps.labels(mount_name=m_name, mount_path=mount_path).set(
                0.0
            )
            continue

        timeout = bool(data.get("timeout", False))
        ok = bool(data.get("ok", False)) and not timeout

        ping_ms = data.get("ping_ms")
        meta_ms = data.get("metadata_ms")
        gbps = data.get("throughput_gbps")

        mount_valid.labels(mount_name=m_name, mount_path=mount_path).set(1 if ok else 0)

        if timeout:
            mount_ping_ms.labels(mount_name=m_name, mount_path=mount_path).set(
                _timeout_ping_ms()
            )
            mount_metadata_latency_ms.labels(
                mount_name=m_name, mount_path=mount_path
            ).set(_timeout_metadata_ms())
            mount_data_rate_gbps.labels(mount_name=m_name, mount_path=mount_path).set(
                0.0
            )
            continue

        if ping_ms is not None:
            mount_ping_ms.labels(mount_name=m_name, mount_path=mount_path).set(
                float(ping_ms)
            )

        if meta_ms is not None:
            mount_metadata_latency_ms.labels(
                mount_name=m_name, mount_path=mount_path
            ).set(float(meta_ms))

        if gbps is not None:
            mount_data_rate_gbps.labels(mount_name=m_name, mount_path=mount_path).set(
                float(gbps)
            )


if __name__ == "__main__":
    start_http_server(8000)
    while True:
        update_metrics()
        time.sleep(CHECK_INTERVAL_S)
