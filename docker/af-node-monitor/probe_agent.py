"""Per-node mount probe supervisor — one DaemonSet pod per (mount, node).

Every cycle runs job_runner.py as a fresh child under a hard deadline and
serves the latest verdict over HTTP, where the exporter pulls it. Three states
stay distinguishable:

  mount is broken      -> the child reports it, or blows its deadline; the
                          supervisor publishes a timeout result (valid=0).
  probe is broken      -> nothing new is published, the last result goes
                          stale, and the pod drops out of Ready (`ready`).
  supervisor is wedged -> the heartbeat stops; the pod drops out of Ready and
                          the liveness probe restarts the container.

Every file this process touches is on the pod's emptyDir: node-local disk, so
nothing here can hang on the storage it probes or on a volume shared with other
probes. The kubelet's probes read the same files, so no network client can fail
them.
"""

import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value  # type: ignore[return-value]


MOUNT_NAME = _get_env("MOUNT_NAME", required=True)
NODE_NAME = os.getenv("NODE_NAME") or ""
RUNTIME_DIR = Path(_get_env("PROBE_RUNTIME_DIR", "/run/af-node-monitor"))
RESULTS_DIR = Path(_get_env("RESULTS_DIR", str(RUNTIME_DIR / "results")))
JOB_RUNNER = _get_env("JOB_RUNNER_PATH", "/scripts/job_runner.py")
CODE = Path(__file__)
HTTP_PORT = int(_get_env("PROBE_HTTP_PORT", "8080"))

PROBE_INTERVAL_S = float(_get_env("PROBE_INTERVAL_S", "600"))
# Ceiling on one attempt: ping + metadata + fio timeouts, with headroom.
PROBE_DEADLINE_S = float(_get_env("PROBE_DEADLINE_S", "180"))
# De-synchronise the fleet so every node does not probe the same server at once.
PROBE_STARTUP_JITTER_S = float(_get_env("PROBE_STARTUP_JITTER_S", "60"))
# How long to wait for a killed child before abandoning it. A child wedged in
# an uninterruptible read cannot be reaped at all; waiting for it is what
# would freeze this loop.
CHILD_KILL_GRACE_S = float(_get_env("CHILD_KILL_GRACE_S", "5"))
# Longest one HTTP connection may take in total, however slowly it sends.
HTTP_TIMEOUT_S = float(_get_env("PROBE_HTTP_TIMEOUT_S", "5"))

HEARTBEAT = RUNTIME_DIR / "heartbeat"
READY = RUNTIME_DIR / "ready"
ATTEMPTS_DIR = RESULTS_DIR / ".attempts"


def _vlog(msg: str) -> None:
    if os.getenv("AF_NODE_MONITOR_VERBOSE", "").lower() in ("1", "true", "yes"):
        print(msg, flush=True)


def _elog(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Published:
    """The verdict the HTTP thread serves and the probe loop replaces."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.body: bytes | None = None

    def set(self, data: Dict[str, Any]) -> None:
        body = json.dumps(data).encode()
        with self.lock:
            self.body = body

    def get(self) -> bytes | None:
        with self.lock:
            return self.body


PUBLISHED = Published()


def result_path() -> Path:
    return RESULTS_DIR / "result.json"


def attempt_path(seq: int) -> Path:
    return ATTEMPTS_DIR / f"{os.getpid()}-{seq}.json"


def reap_orphans() -> None:
    """Reap children abandoned by a previous cycle.

    A child killed while its own grandchild sits in uninterruptible sleep
    leaves that grandchild reparented to this process, which is PID 1 in the
    container. Nothing else will ever reap it.
    """
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except Exception as e:  # pragma: no cover - defensive
            _elog(f"[probe_agent] reap failed: {e}")
            return
        if pid == 0:
            return
        _vlog(f"[probe_agent] reaped orphan pid={pid}")


def sweep_attempts(keep: Path | None) -> None:
    """Drop attempt files this pod is no longer waiting on, including leftovers
    from earlier container incarnations, which share the emptyDir."""
    try:
        entries = list(ATTEMPTS_DIR.iterdir())
    except FileNotFoundError:
        return
    except OSError as e:
        _elog(f"[probe_agent] cannot list {ATTEMPTS_DIR}: {e}")
        return
    for entry in entries:
        if keep is not None and entry.name in (keep.name, keep.name + ".tmp"):
            continue
        try:
            entry.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            _elog(f"[probe_agent] cannot remove {entry}: {e}")


def load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            loaded: Dict[str, Any] = json.load(f)
            return loaded
    except FileNotFoundError:
        return {}
    except Exception as e:
        _elog(f"[probe_agent] cannot read {path}: {e}")
        return {}


def write_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f)
    tmp.replace(path)


def touch(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, None)
    except OSError as e:
        _elog(f"[probe_agent] cannot touch {path}: {e}")


def set_ready(ready: bool) -> None:
    """Ready means the probe machinery works, not that the mount is good.

    A mount that times out is a *successful* probe: it published a verdict.
    Only a probe that cannot produce one drops out of Ready, which is what
    separates "storage is down" from "monitoring is down" on
    af_node_mount_probe_up.
    """
    if ready:
        touch(READY)
        return
    try:
        READY.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        _elog(f"[probe_agent] cannot clear {READY}: {e}")


def child_env(attempt: Path) -> Dict[str, str]:
    env = dict(os.environ)
    env["RESULT_PATH"] = str(attempt)
    env["PREV_RESULT_PATH"] = str(result_path())
    env["RESULTS_DIR"] = str(RESULTS_DIR)
    return env


def timeout_result(prev: Dict[str, Any]) -> Dict[str, Any]:
    """The shape job_runner writes when a check times out.

    ping_ms/metadata_ms are left null on purpose: the exporter substitutes its
    own timeout sentinels, and a partial measurement from a wedged read is not
    a latency anyone should chart. last_fio_ts and the last measured rate carry
    over, so a recovering mount neither runs fio on every cycle nor reports a
    rate of zero it never measured.
    """
    return {
        "timestamp": time.time(),
        "ok": False,
        "timeout": True,
        "ping_ms": None,
        "metadata_ms": None,
        "throughput_gbps": prev.get("throughput_gbps"),
        "last_fio_ts": prev.get("last_fio_ts"),
        "node": NODE_NAME or "",
    }


def run_attempt(seq: int) -> str:
    """Run one check. Returns "published", "timeout" or "failed"."""
    attempt = attempt_path(seq)
    sweep_attempts(keep=attempt)

    try:
        ATTEMPTS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _elog(f"[probe_agent] cannot create {ATTEMPTS_DIR}: {e}")
        return "failed"

    try:
        proc = subprocess.Popen(
            [sys.executable, JOB_RUNNER],
            env=child_env(attempt),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:
        _elog(f"[probe_agent] cannot start {JOB_RUNNER}: {e}")
        return "failed"

    timed_out = False
    try:
        _out, err = proc.communicate(timeout=PROBE_DEADLINE_S)
    except subprocess.TimeoutExpired:
        timed_out = True
        err = ""
        proc.kill()
        try:
            proc.communicate(timeout=CHILD_KILL_GRACE_S)
        except Exception:
            # Wedged in the kernel; it will reparent here and be reaped later.
            _elog(f"[probe_agent] child pid={proc.pid} unkillable, abandoned")
    except Exception as e:
        _elog(f"[probe_agent] child failed: {e}")
        return "failed"

    if timed_out:
        result = timeout_result(load_json(result_path()))
        try:
            write_atomic(result_path(), result)
        except OSError as e:
            _elog(f"[probe_agent] cannot record timeout result: {e}")
            return "failed"
        PUBLISHED.set(result)
        _vlog(f"[probe_agent] {MOUNT_NAME}: deadline exceeded, published timeout")
        return "timeout"

    if proc.returncode != 0:
        # The check itself broke (bad config, crash). Publishing anything here
        # would be a guess; let the last result go stale and drop out of Ready.
        _elog(
            f"[probe_agent] {MOUNT_NAME}: check exited {proc.returncode}: "
            f"{(err or '').strip()[:500]}"
        )
        return "failed"

    result = load_json(attempt)
    if not result:
        _elog(f"[probe_agent] {MOUNT_NAME}: check produced no result")
        return "failed"

    try:
        # Promoted from this attempt's own file, which no abandoned child writes.
        os.replace(attempt, result_path())
    except OSError as e:
        _elog(f"[probe_agent] cannot record result: {e}")
        return "failed"
    PUBLISHED.set(result)
    return "published"


def cycle(seq: int) -> str:
    reap_orphans()
    touch(HEARTBEAT)
    try:
        outcome = run_attempt(seq)
    except Exception as e:
        # Handled here, not in main(): a probe that crashed must stop
        # advertising itself as Ready, and that invariant should not depend on
        # the caller remembering it.
        _elog(f"[probe_agent] {MOUNT_NAME}: attempt raised: {e}")
        outcome = "failed"
    set_ready(outcome in ("published", "timeout"))
    touch(HEARTBEAT)
    return outcome


def start() -> None:
    """Take over from an earlier container of this pod.

    Its verdict is served until the first cycle replaces it, but its Ready
    marker is not inherited: this container is Ready once it has produced a
    verdict of its own.
    """
    set_ready(False)
    touch(HEARTBEAT)
    prev = load_json(result_path())
    if prev:
        PUBLISHED.set(prev)


class Handler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        self._deadline = threading.Timer(HTTP_TIMEOUT_S, self._cut)
        self._deadline.daemon = True
        self._deadline.start()

    def _cut(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self) -> None:
        self._deadline.cancel()
        try:
            super().finish()
        except OSError:
            pass

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        body = PUBLISHED.get() if self.path == "/result" else None
        if body is None:
            self._send(404, b"not found\n", "text/plain")
        else:
            self._send(200, body, "application/json")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        _vlog(f"[probe_agent] http {self.address_string()} {format % args}")


class Server(HTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        _vlog(f"[probe_agent] http {client_address}: connection dropped")


def serve(port: int = HTTP_PORT) -> HTTPServer:
    # One thread, each connection cut at HTTP_TIMEOUT_S: memory stays bounded.
    server = Server(("", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    """Probe until kubelet swaps other code than this into the mounted ConfigMap,
    exiting only between cycles. job_runner.py needs no restart: each cycle runs it afresh."""
    running = CODE.read_bytes()
    start()
    serve()
    # The jitter goes after the first cycle, not before it: a pod that has to
    # wait to report is a pod that stays NotReady, and a DaemonSet rollout
    # advances one wave at a time on readiness.
    jitter = random.uniform(0, PROBE_STARTUP_JITTER_S)
    seq = 0
    while CODE.read_bytes() == running:
        started = time.time()
        cycle(seq)
        delay = PROBE_INTERVAL_S + (jitter if seq == 0 else 0.0)
        seq += 1
        # Pace on cycle starts, not on cycle ends, so a slow check does not
        # stretch the interval past the exporter's staleness window.
        time.sleep(max(0.0, started + delay - time.time()))
    print(
        f"[probe_agent] {CODE} changed: exiting, for the container to restart on it",
        flush=True,
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
