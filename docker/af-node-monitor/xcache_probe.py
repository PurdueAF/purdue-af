"""Read one file through XCache with the facility's grid proxy and export the verdict."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from prometheus_client import Gauge, start_http_server

CODE = Path(__file__)

XCACHE_PROBE_URL = os.getenv("XCACHE_PROBE_URL", "")
CHECK_INTERVAL_S = float(os.getenv("CHECK_INTERVAL_S", "300"))
READ_TIMEOUT_S = float(os.getenv("READ_TIMEOUT_S", "120"))

read_ok = Gauge(
    "af_xcache_read_ok",
    "1 when the last read of the probe file through XCache completed, 0 when "
    "it failed or timed out",
)
read_duration = Gauge(
    "af_xcache_read_duration_seconds",
    "Duration of the last read of the probe file through XCache",
)
last_read_ts = Gauge(
    "af_xcache_last_read_timestamp_seconds",
    "Unix timestamp of the last finished read of the probe file",
)


def read_file(url: str) -> bool | None:
    """True or False for a read that ran; None when no client could run it."""
    try:
        result = subprocess.run(
            ["xrdcp", "--force", "--nopbar", url, "/dev/null"],
            capture_output=True,
            text=True,
            timeout=READ_TIMEOUT_S,
        )
    except FileNotFoundError:
        print("[xcache_probe] No xrdcp in this image: not reading")
        return None
    except subprocess.TimeoutExpired:
        print(f"[xcache_probe] Read of {url} gave no result in {READ_TIMEOUT_S} s")
        return False
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        print(f"[xcache_probe] Read of {url} failed: {lines[-1] if lines else ''}")
        return False
    return True


def probe_once() -> None:
    started = time.monotonic()
    verdict = read_file(XCACHE_PROBE_URL)
    if verdict is None:
        return
    read_ok.set(verdict)
    read_duration.set(time.monotonic() - started)
    last_read_ts.set(time.time())


def main() -> None:
    """Export until kubelet swaps other code than this into the mounted ConfigMap."""
    running = CODE.read_bytes()
    start_http_server(8000)
    while CODE.read_bytes() == running:
        probe_once()
        time.sleep(CHECK_INTERVAL_S)
    print(f"[xcache_probe] {CODE} changed: exiting, for the container to restart on it")


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
