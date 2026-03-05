import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value  # type: ignore[return-value]


# Configuration from environment (per-mount worker)
MOUNT_NAME = _get_env("MOUNT_NAME", required=True)
CHECK_FILE = _get_env("CHECK_FILE", required=True)
CHECKSUM = _get_env("CHECKSUM", required=False)
METADATA_DIR = _get_env("METADATA_DIR", required=False)
FIO_FILE = _get_env("FIO_FILE", required=False)

PING_TIMEOUT_S = float(_get_env("PING_TIMEOUT_S", "3"))
METADATA_TIMEOUT_S = float(_get_env("METADATA_TIMEOUT_S", "10"))
FIO_TIMEOUT_S = float(_get_env("FIO_TIMEOUT_S", "120"))

ENABLE_FIO = _get_env("ENABLE_FIO", "false").lower() in {"1", "true", "yes"}

SERVER_PORT = int(_get_env("WORKER_PORT", "8080"))


def check_ping() -> tuple[bool, bool, float | None]:
    """Return (ok, timeout, ping_ms)."""
    start = time.time()
    try:
        if not CHECKSUM:
            # If no checksum is configured, just test existence / readability via cat
            result = subprocess.run(
                ["cat", CHECK_FILE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=PING_TIMEOUT_S,
            )
        else:
            result = subprocess.run(
                ["/usr/bin/md5sum", CHECK_FILE],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=PING_TIMEOUT_S,
            )
        elapsed_ms = (time.time() - start) * 1000

        if result.returncode != 0:
            return False, False, elapsed_ms

        if CHECKSUM:
            parts = result.stdout.strip().split()
            if not parts:
                return False, False, elapsed_ms
            checksum = parts[0]
            if checksum != CHECKSUM:
                return False, False, elapsed_ms

        return True, False, elapsed_ms
    except subprocess.TimeoutExpired:
        elapsed_ms = (time.time() - start) * 1000
        return False, True, elapsed_ms
    except Exception:
        elapsed_ms = (time.time() - start) * 1000
        return False, False, elapsed_ms


def check_metadata() -> tuple[bool, bool, float | None]:
    """Return (ok, timeout, latency_ms)."""
    if not METADATA_DIR:
        return True, False, None

    start = time.time()
    try:
        result = subprocess.run(
            ["ls", "-la", METADATA_DIR],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=METADATA_TIMEOUT_S,
        )
        elapsed_ms = (time.time() - start) * 1000
        if result.returncode != 0:
            return False, False, elapsed_ms
        return True, False, elapsed_ms
    except subprocess.TimeoutExpired:
        elapsed_ms = (time.time() - start) * 1000
        return False, True, elapsed_ms
    except Exception:
        elapsed_ms = (time.time() - start) * 1000
        return False, False, elapsed_ms


def check_throughput() -> tuple[bool, bool, float | None]:
    """Return (ok, timeout, gbps)."""
    if not ENABLE_FIO or not FIO_FILE:
        return True, False, None

    try:
        result = subprocess.run(
            [
                "fio",
                "--name=read_test",
                f"--filename={FIO_FILE}",
                "--rw=read",
                "--bs=1M",
                "--size=1G",
                "--numjobs=1",
                "--readonly",
                "--output-format=json",
            ],
            capture_output=True,
            text=True,
            timeout=FIO_TIMEOUT_S,
        )
        if result.returncode != 0:
            return False, False, None
        data = json.loads(result.stdout)
        bw_bytes = data["jobs"][0]["read"]["bw_bytes"]
        gbps = bw_bytes / 1e9
        return True, False, gbps
    except subprocess.TimeoutExpired:
        return False, True, None
    except Exception:
        return False, False, None


class Handler(BaseHTTPRequestHandler):
    def _write_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz" or self.path == "/readyz":
            # Very lightweight self-check: just report mount name
            self._write_json(200, {"status": "ok", "mount": MOUNT_NAME})
            return

        if self.path != "/check":
            self._write_json(404, {"error": "not found"})
            return

        ping_ok, ping_timeout, ping_ms = check_ping()
        meta_ok, meta_timeout, meta_ms = check_metadata()
        fio_ok, fio_timeout, gbps = check_throughput()

        timeout = ping_timeout or meta_timeout or fio_timeout
        ok = ping_ok and meta_ok and fio_ok and not timeout

        resp = {
            "mount_name": MOUNT_NAME,
            "ok": ok,
            "timeout": timeout,
            "ping_ms": ping_ms,
            "metadata_ms": meta_ms,
            "throughput_gbps": gbps,
        }
        self._write_json(200, resp)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Reduce noise in logs
        return


def run_server() -> None:
    server = HTTPServer(("0.0.0.0", SERVER_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    run_server()

