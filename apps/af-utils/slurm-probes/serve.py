#!/usr/bin/env python3
"""Serve the probe textfiles as one Prometheus endpoint.

The probes run in the interlink-slurm-plugin image, which has no Python, so
they write node_exporter-style textfiles into a shared emptyDir and this
sidecar concatenates them. Missing or stale files are simply absent from the
output rather than served as zeros: a probe that has not reported says
nothing, which is not the same as "no backlog".
"""

from __future__ import annotations

import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

METRICS_DIR = Path(os.environ.get("PROBE_OUT_DIR", "/var/lib/slurm-probes"))
PORT = int(os.environ.get("PROBE_PORT", "9100"))
# A probe writes every PROBE_INTERVAL_S; treat anything much older as gone.
MAX_AGE_S = float(os.environ.get("PROBE_MAX_AGE_S", "1800"))


def collect() -> str:
    """Concatenate fresh textfiles, de-duplicating HELP/TYPE headers."""
    seen: set[str] = set()
    body: list[str] = []
    now = time.time()
    for path in sorted(METRICS_DIR.glob("*.prom")):
        try:
            if now - path.stat().st_mtime > MAX_AGE_S:
                continue
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("#"):
                # one HELP/TYPE per metric name across all cluster files
                if line in seen:
                    continue
                seen.add(line)
            body.append(line)
    return "\n".join(body) + "\n" if body else ""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.rstrip("/") in ("/healthz",):
            payload = b"ok\n"
        elif self.path.rstrip("/") in ("", "/metrics"):
            payload = collect().encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        """Scrapes every 15s would otherwise bury the probes' own output."""


def main() -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"serving {METRICS_DIR} on :{PORT}", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
