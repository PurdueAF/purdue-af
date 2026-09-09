"""Per-node mount probe supervisor — one DaemonSet pod per (mount, node).

Replaces the Job-per-mount-per-node factory that node_healthcheck.py used to
run. Every cycle still executes job_runner.py as a *fresh* child under a hard
deadline, so the check semantics are unchanged; what goes away is 68 Pods per
10 minutes and their scheduling, image pulls and API churn.

Three states have to stay distinguishable, because collapsing any two of them
turns a real outage into "unknown" or an unknown into a false alarm:

  mount is broken      -> the child reports it, or blows its deadline; the
                          supervisor publishes a timeout result (valid=0).
  probe is broken      -> nothing is published, the last result goes stale,
                          and the pod drops out of Ready (`healthy` marker).
  supervisor is wedged -> the heartbeat stops and the liveness probe restarts
                          the container.
"""

import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value  # type: ignore[return-value]


MOUNT_NAME = _get_env("MOUNT_NAME", required=True)
NODE_NAME = os.getenv("NODE_NAME") or ""
RESULTS_DIR = Path(_get_env("RESULTS_DIR", "/af-node-monitor/results"))
RUNTIME_DIR = Path(_get_env("PROBE_RUNTIME_DIR", "/run/af-node-monitor"))
JOB_RUNNER = _get_env("JOB_RUNNER_PATH", "/scripts/job_runner.py")

PROBE_INTERVAL_S = float(_get_env("PROBE_INTERVAL_S", "600"))
# Ceiling on one attempt: ping 3s + metadata 10s + fio 120s, with headroom.
# Matches the activeDeadlineSeconds the Jobs used to carry.
PROBE_DEADLINE_S = float(_get_env("PROBE_DEADLINE_S", "180"))
# De-synchronise the fleet: without it every node reads 1 GiB off the same
# server in the same second on every fio cycle.
PROBE_STARTUP_JITTER_S = float(_get_env("PROBE_STARTUP_JITTER_S", "60"))
# How long to wait for a killed child before abandoning it. A child wedged in
# an uninterruptible read cannot be reaped at all; waiting for it is what
# would freeze this loop.
CHILD_KILL_GRACE_S = float(_get_env("CHILD_KILL_GRACE_S", "5"))

HEARTBEAT = RUNTIME_DIR / "heartbeat"
HEALTHY = RUNTIME_DIR / "healthy"


def _vlog(msg: str) -> None:
    if os.getenv("AF_NODE_MONITOR_VERBOSE", "").lower() in ("1", "true", "yes"):
        print(msg, flush=True)


def _elog(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _sanitized_mount_name(name: str) -> str:
    return name.strip("/").replace("/", "_") or "root"


def _sanitized_node_name(name: str) -> str:
    return name.strip().replace("/", "_") if name else ""


MOUNT_KEY = _sanitized_mount_name(MOUNT_NAME)
NODE_KEY = _sanitized_node_name(NODE_NAME)
STEM = f"{MOUNT_KEY}__{NODE_KEY}" if NODE_KEY else MOUNT_KEY

# Attempts land in their own directory on the same filesystem, so promoting one
# is an atomic rename and a late-waking child can never land on the published
# path. os.replace() across .attempts/ and results/ stays atomic because both
# sit under RESULTS_DIR.
ATTEMPTS_DIR = RESULTS_DIR / ".attempts"


def result_path() -> Path:
    return RESULTS_DIR / f"{STEM}.json"


def attempt_path(seq: int) -> Path:
    return ATTEMPTS_DIR / f"{STEM}-{os.getpid()}-{seq}.json"


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
    """Drop attempt files this pod is no longer waiting on.

    Covers leftovers from earlier pod incarnations too — same stem, different
    pid — so a crashlooping probe cannot fill the results PVC.
    """
    try:
        entries = list(ATTEMPTS_DIR.iterdir())
    except FileNotFoundError:
        return
    except OSError as e:
        _elog(f"[probe_agent] cannot list {ATTEMPTS_DIR}: {e}")
        return
    for entry in entries:
        if not entry.name.startswith(f"{STEM}-"):
            continue
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


def set_healthy(healthy: bool) -> None:
    """Ready means the probe machinery works, not that the mount is good.

    A mount that times out is a *successful* probe: it published a verdict.
    Only a probe that cannot produce one at all drops out of Ready, which is
    what separates "storage is down" from "monitoring is down" on the
    af_node_mount_probe_up gauge.
    """
    if healthy:
        touch(HEALTHY)
        return
    try:
        HEALTHY.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        _elog(f"[probe_agent] cannot clear {HEALTHY}: {e}")


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
    a latency anyone should chart. last_fio_ts carries over so a recovering
    mount does not get an fio run on every cycle.
    """
    return {
        "timestamp": time.time(),
        "ok": False,
        "timeout": True,
        "ping_ms": None,
        "metadata_ms": None,
        "throughput_gbps": 0.0,
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
        # Results storage is unreachable. Publishing a timeout here would blame
        # the monitored mount for a fault in the results PVC.
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
        prev = load_json(result_path())
        try:
            write_atomic(result_path(), timeout_result(prev))
        except OSError as e:
            _elog(f"[probe_agent] cannot publish timeout result: {e}")
            return "failed"
        _vlog(f"[probe_agent] {MOUNT_NAME}: deadline exceeded, published timeout")
        return "timeout"

    if proc.returncode != 0:
        # The check itself broke (bad config, crash, unwritable results PVC).
        # Publishing anything here would be a guess; let the last result go
        # stale instead and drop out of Ready.
        _elog(
            f"[probe_agent] {MOUNT_NAME}: check exited {proc.returncode}: "
            f"{(err or '').strip()[:500]}"
        )
        return "failed"

    if not attempt.exists():
        _elog(f"[probe_agent] {MOUNT_NAME}: check produced no result file")
        return "failed"

    try:
        os.replace(attempt, result_path())
    except OSError as e:
        _elog(f"[probe_agent] cannot publish result: {e}")
        return "failed"
    return "published"


def cycle(seq: int) -> str:
    reap_orphans()
    touch(HEARTBEAT)
    outcome = run_attempt(seq)
    set_healthy(outcome in ("published", "timeout"))
    touch(HEARTBEAT)
    return outcome


def main() -> None:  # pragma: no cover - process entrypoint
    # The jitter goes after the first cycle, not before it: a pod that has to
    # wait to report is a pod that stays NotReady, and a DaemonSet rollout
    # advances one wave at a time on readiness.
    jitter = random.uniform(0, PROBE_STARTUP_JITTER_S)
    seq = 0
    while True:
        started = time.time()
        try:
            cycle(seq)
        except Exception as e:
            _elog(f"[probe_agent] cycle failed: {e}")
            set_healthy(False)
        delay = PROBE_INTERVAL_S + (jitter if seq == 0 else 0.0)
        seq += 1
        # Pace on cycle starts, not on cycle ends, so a slow check does not
        # stretch the interval past the exporter's staleness window.
        time.sleep(max(0.0, started + delay - time.time()))


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
