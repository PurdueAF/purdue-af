import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Tuple


def _af_node_monitor_verbose() -> bool:
    return os.getenv("AF_NODE_MONITOR_VERBOSE", "").lower() in ("1", "true", "yes")


# When quiet, redirect OS fds 1/2 so inherited stdio and os.write(2, ...)
# don't pollute Job logs (kubectl / Loki). Replacing sys.stdout alone is not enough.
if not _af_node_monitor_verbose():
    _dn = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_dn, 1)
    os.dup2(_dn, 2)
    if _dn > 2:
        os.close(_dn)
    sys.stdout = open(1, "w", encoding="utf-8", closefd=False)
    sys.stderr = open(2, "w", encoding="utf-8", closefd=False)


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value  # type: ignore[return-value]


MOUNT_NAME = _get_env("MOUNT_NAME", required=True)
CHECK_FILE = _get_env("CHECK_FILE", required=True)
CHECKSUM = _get_env("CHECKSUM", required=False)
METADATA_DIR = _get_env("METADATA_DIR", required=False)
FIO_FILE = _get_env("FIO_FILE", required=False)

PING_TIMEOUT_S = float(_get_env("PING_TIMEOUT_S", "3"))
METADATA_TIMEOUT_S = float(_get_env("METADATA_TIMEOUT_S", "10"))
FIO_TIMEOUT_S = float(_get_env("FIO_TIMEOUT_S", "120"))
FIO_INTERVAL_S = float(_get_env("FIO_INTERVAL_S", "1800"))  # 30 minutes default

ENABLE_FIO = _get_env("ENABLE_FIO", "false").lower() in {"1", "true", "yes"}

RESULTS_DIR = Path(_get_env("RESULTS_DIR", "/af-node-monitor/results"))
NODE_NAME = os.getenv("NODE_NAME") or ""

# Set by probe_agent: where this attempt writes, and which file carries the
# last_fio_ts it must honour. Unset means derive both, as standalone runs do.
RESULT_PATH = os.getenv("RESULT_PATH") or ""
PREV_RESULT_PATH = os.getenv("PREV_RESULT_PATH") or ""


def _vlog(msg: str) -> None:
    print(msg)


def _sanitized_mount_name(name: str) -> str:
    # Replace slashes and other problematic chars for filesystem paths.
    return name.strip("/").replace("/", "_") or "root"


def _sanitized_node_name(name: str) -> str:
    return name.strip().replace("/", "_") if name else ""


def _load_previous_result(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            loaded: Dict[str, Any] = json.load(f)
            return loaded
    except Exception:
        return {}


def _write_result_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f)
    tmp_path.replace(path)


# A read on a dead NFS/CephFS mount sits in uninterruptible sleep: SIGKILL is
# recorded but not delivered until the syscall returns, so subprocess.run()'s
# own timeout path (kill() then an unbounded wait()) never comes back. Every
# check here is on exactly such a mount, so waits must be bounded and the
# child abandoned rather than reaped. Orphans reparent to the supervisor,
# which reaps them (see probe_agent.reap_orphans).
def _run_bounded(cmd: list[str], timeout_s: float) -> Tuple[bool, bool, str, str]:
    """Return (ok, timeout, stdout, stderr_or_reason). Never blocks past timeout_s."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:
        return False, False, "", str(e)

    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            # Bounded: a killable child dies at once, a D-state one never will.
            out, err = proc.communicate(timeout=1)
        except Exception:
            pass
        return False, True, "", "timeout"
    except Exception as e:
        return False, False, "", str(e)

    if proc.returncode != 0:
        return False, False, out, err.strip()
    return True, False, out, ""


def _run_subprocess(cmd: list[str], timeout_s: float) -> Tuple[bool, bool, str]:
    """Return (ok, timeout, stderr_or_reason)."""
    ok, timeout, _out, reason = _run_bounded(cmd, timeout_s)
    return ok, timeout, reason


def _check_ping() -> Tuple[bool, bool, float | None]:
    start = time.time()
    if CHECKSUM:
        cmd = ["/usr/bin/md5sum", CHECK_FILE]
    else:
        cmd = ["cat", CHECK_FILE]
    ok, timeout, out, reason = _run_bounded(cmd, PING_TIMEOUT_S)
    elapsed_ms = (time.time() - start) * 1000
    if not ok or timeout:
        msg = "[job_runner] Ping check failed"
        if timeout:
            msg += f": timeout after {elapsed_ms:.1f} ms"
        elif reason:
            msg += f": {reason}"
        else:
            msg += ": non-zero exit code"
        _vlog(msg)
        return False, timeout, elapsed_ms

    if CHECKSUM:
        # stdout of the run we just timed — re-reading the file would double
        # the load and could hang after the timed read already succeeded.
        parts = out.strip().split()
        if not parts or parts[0] != CHECKSUM:
            actual = parts[0] if parts else "<missing>"
            _vlog(
                "[job_runner] Ping checksum mismatch: "
                f"expected={CHECKSUM}, actual={actual}"
            )
            return False, False, elapsed_ms

    return True, False, elapsed_ms


def _check_metadata() -> Tuple[bool, bool, float | None]:
    if not METADATA_DIR:
        return True, False, None
    start = time.time()
    ok, timeout, reason = _run_subprocess(
        ["ls", "-la", METADATA_DIR],
        METADATA_TIMEOUT_S,
    )
    elapsed_ms = (time.time() - start) * 1000
    if not ok or timeout:
        msg = "[job_runner] Metadata check failed"
        if timeout:
            msg += f": timeout after {elapsed_ms:.1f} ms"
        elif reason:
            msg += f": {reason}"
        else:
            msg += ": non-zero exit code"
        _vlog(msg)
    return ok, timeout, elapsed_ms


def _check_throughput(
    last_fio_ts: float | None,
) -> Tuple[bool, bool, float | None, float | None]:
    """Return (ok, timeout, gbps, new_last_fio_ts)."""
    now = time.time()

    if not ENABLE_FIO or not FIO_FILE:
        return True, False, None, last_fio_ts

    if last_fio_ts is not None and (now - last_fio_ts) < FIO_INTERVAL_S:
        # Too soon to run fio again; reuse previous throughput if any.
        return True, False, None, last_fio_ts

    cmd = [
        "fio",
        "--name=read_test",
        f"--filename={FIO_FILE}",
        "--rw=read",
        "--bs=1M",
        "--size=1G",
        "--numjobs=1",
        "--readonly",
        "--output-format=json",
    ]
    ok, timeout, out, _reason = _run_bounded(cmd, FIO_TIMEOUT_S)
    if timeout:
        return False, True, 0.0, last_fio_ts
    if not ok:
        return False, False, 0.0, last_fio_ts
    try:
        data = json.loads(out)
        bw_bytes = data["jobs"][0]["read"]["bw_bytes"]
        gbps = bw_bytes / 1e9
    except Exception:
        return False, False, 0.0, last_fio_ts
    return True, False, gbps, now


def default_result_path() -> Path:
    mount_key = _sanitized_mount_name(MOUNT_NAME)
    node_key = _sanitized_node_name(NODE_NAME)
    if node_key:
        return RESULTS_DIR / f"{mount_key}__{node_key}.json"
    # Backwards-compatible path for legacy CronJobs without NODE_NAME.
    return RESULTS_DIR / f"{mount_key}.json"


def main() -> None:
    result_path = Path(RESULT_PATH) if RESULT_PATH else default_result_path()
    prev_path = Path(PREV_RESULT_PATH) if PREV_RESULT_PATH else result_path

    _vlog(
        f"[job_runner] Starting checks for mount '{MOUNT_NAME}' "
        f"(result='{result_path}') "
        f"on node '{NODE_NAME or 'unknown'}'"
    )
    _vlog(
        f"[job_runner] CHECK_FILE={CHECK_FILE}, METADATA_DIR={METADATA_DIR}, FIO_FILE={FIO_FILE}, ENABLE_FIO={ENABLE_FIO}"
    )

    prev = _load_previous_result(prev_path)
    last_fio_ts = prev.get("last_fio_ts")
    try:
        last_fio_ts = float(last_fio_ts) if last_fio_ts is not None else None
    except Exception:
        last_fio_ts = None

    if prev:
        _vlog(
            f"[job_runner] Previous result: ok={prev.get('ok')}, timeout={prev.get('timeout')}, "
            f"throughput_gbps={prev.get('throughput_gbps')}, last_fio_ts={last_fio_ts}"
        )

    # Responsiveness first.
    ping_ok, ping_timeout, ping_ms = _check_ping()
    meta_ok, meta_timeout, meta_ms = _check_metadata()

    _vlog(
        f"[job_runner] Ping result: ok={ping_ok}, timeout={ping_timeout}, ping_ms={ping_ms}"
    )
    _vlog(
        f"[job_runner] Metadata result: ok={meta_ok}, timeout={meta_timeout}, metadata_ms={meta_ms}"
    )

    timeout = ping_timeout or meta_timeout
    ok = ping_ok and meta_ok and not timeout

    now = time.time()
    throughput_gbps: float | None = prev.get("throughput_gbps")

    if not ok:
        # Responsiveness failed or timed out; report timeout with speed 0.
        timeout = True
        _vlog(
            "[job_runner] Responsiveness failed or timed out; reporting timeout with throughput 0.0"
        )
        result = {
            "timestamp": now,
            "ok": False,
            "timeout": True,
            "ping_ms": ping_ms,
            "metadata_ms": meta_ms,
            "throughput_gbps": 0.0,
            "last_fio_ts": last_fio_ts,
            "node": NODE_NAME or "",
        }
        _write_result_atomic(result_path, result)
        return

    # Responsiveness ok; decide whether to run fio.
    will_run_fio = (
        ENABLE_FIO
        and FIO_FILE
        and (last_fio_ts is None or (now - last_fio_ts) >= FIO_INTERVAL_S)
    )
    _vlog(
        f"[job_runner] FIO decision: ENABLE_FIO={ENABLE_FIO}, FIO_FILE={FIO_FILE}, "
        f"last_fio_ts={last_fio_ts}, will_run_fio={bool(will_run_fio)}"
    )

    fio_ok, fio_timeout, fio_gbps, new_last_fio_ts = _check_throughput(last_fio_ts)
    if new_last_fio_ts is not None:
        last_fio_ts = new_last_fio_ts

    if fio_gbps is not None:
        throughput_gbps = fio_gbps

    timeout = fio_timeout
    ok = ok and fio_ok and not timeout

    _vlog(
        f"[job_runner] FIO result: ok={fio_ok}, timeout={fio_timeout}, "
        f"throughput_gbps={fio_gbps}, new_last_fio_ts={new_last_fio_ts}"
    )

    result = {
        "timestamp": now,
        "ok": ok,
        "timeout": timeout,
        "ping_ms": ping_ms,
        "metadata_ms": meta_ms,
        "throughput_gbps": (
            float(throughput_gbps) if throughput_gbps is not None else 0.0
        ),
        "last_fio_ts": last_fio_ts,
        "node": NODE_NAME or "",
    }
    _vlog(f"[job_runner] Final result for '{MOUNT_NAME}': {result}")
    _write_result_atomic(result_path, result)


if __name__ == "__main__":
    main()
