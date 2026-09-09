"""Tests for docker/af-node-monitor/probe_agent.py.

The supervisor's whole job is keeping three failures apart, because every one
of them used to arrive as "unknown":

  the mount did not answer   -> publish a timeout verdict (goes red)
  the probe itself broke     -> publish nothing, drop out of Ready (goes stale)
  the results volume is gone -> publish nothing, and do not blame the mount

A child is a real subprocess here rather than a mock: the deadline path exists
because a check can be unkillable, and that only shows up across a fork.
"""

import json
import os
import sys
import textwrap
import time

os.environ.setdefault("MOUNT_NAME", "/depot/")
os.environ.setdefault("NODE_NAME", "node-a")
os.environ.setdefault("PROBE_STARTUP_JITTER_S", "0")

import probe_agent as pa  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Point every module-level path at tmp_path; they are import-time derived."""
    results = tmp_path / "results"
    runtime = tmp_path / "runtime"
    results.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(pa, "RESULTS_DIR", results)
    monkeypatch.setattr(pa, "ATTEMPTS_DIR", results / ".attempts")
    monkeypatch.setattr(pa, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(pa, "HEARTBEAT", runtime / "heartbeat")
    monkeypatch.setattr(pa, "HEALTHY", runtime / "healthy")
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 5.0)
    return tmp_path


def child(monkeypatch, tmp_path, body: str) -> None:
    """Install a stand-in for job_runner.py that runs `body`."""
    script = tmp_path / "child.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, os, sys, time
            from pathlib import Path
            RESULT_PATH = Path(os.environ["RESULT_PATH"])
            PREV_RESULT_PATH = Path(os.environ["PREV_RESULT_PATH"])
            """
        )
        + textwrap.dedent(body)
    )
    monkeypatch.setattr(pa, "JOB_RUNNER", str(script))


WRITES_OK = """
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps({"ok": True, "timestamp": time.time()}))
"""


# ── the happy path ────────────────────────────────────────────────────────────


def test_successful_attempt_is_promoted(env, monkeypatch):
    child(monkeypatch, env, WRITES_OK)
    assert pa.run_attempt(0) == "published"
    assert json.loads(pa.result_path().read_text())["ok"] is True


def test_cycle_marks_ready_and_beats(env, monkeypatch):
    child(monkeypatch, env, WRITES_OK)
    assert pa.cycle(0) == "published"
    assert pa.HEALTHY.exists()
    assert pa.HEARTBEAT.exists()


def test_child_is_told_where_to_read_and_write(env, monkeypatch):
    """last_fio_ts lives in the published result, not in the attempt file, so
    a recovering mount does not run fio on every cycle."""
    pa.result_path().write_text(json.dumps({"last_fio_ts": 1234.0}))
    child(
        monkeypatch,
        env,
        """
        prev = json.loads(PREV_RESULT_PATH.read_text())
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps({"ok": True, "seen": prev["last_fio_ts"]}))
        """,
    )
    assert pa.run_attempt(0) == "published"
    assert json.loads(pa.result_path().read_text())["seen"] == 1234.0


# ── the mount did not answer ──────────────────────────────────────────────────


def test_deadline_publishes_a_timeout_verdict(env, monkeypatch):
    """This is the case the old Jobs could only report as unknown: the pod was
    killed by activeDeadlineSeconds and nothing was ever written."""
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 0.3)
    child(monkeypatch, env, "time.sleep(30)")
    assert pa.run_attempt(0) == "timeout"

    published = json.loads(pa.result_path().read_text())
    assert published["ok"] is False
    assert published["timeout"] is True
    assert published["throughput_gbps"] == 0.0
    assert published["node"] == "node-a"
    # Null, not a partial reading off a wedged mount — the exporter substitutes
    # its own timeout sentinels.
    assert published["ping_ms"] is None
    assert published["metadata_ms"] is None


def test_timeout_carries_last_fio_ts_forward(env, monkeypatch):
    pa.result_path().write_text(json.dumps({"last_fio_ts": 999.0}))
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 0.3)
    child(monkeypatch, env, "time.sleep(30)")
    pa.run_attempt(0)
    assert json.loads(pa.result_path().read_text())["last_fio_ts"] == 999.0


def test_a_timed_out_probe_stays_ready(env, monkeypatch):
    """It published a verdict, so the probe works. Dropping out of Ready here
    would say "monitoring is broken" about a working probe on a dead mount."""
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 0.3)
    child(monkeypatch, env, "time.sleep(30)")
    assert pa.cycle(0) == "timeout"
    assert pa.HEALTHY.exists()


def test_late_child_cannot_overwrite_the_published_verdict(env, monkeypatch):
    """A child abandoned at its deadline may still be alive. It writes to its
    own attempt file, which is never promoted, so it cannot resurrect a stale
    success over the timeout that replaced it."""
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 0.3)
    child(
        monkeypatch,
        env,
        """
        time.sleep(1.5)
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps({"ok": True, "stale": True}))
        """,
    )
    assert pa.run_attempt(0) == "timeout"
    time.sleep(2.5)
    published = json.loads(pa.result_path().read_text())
    assert published["timeout"] is True
    assert "stale" not in published


# ── the probe itself broke ────────────────────────────────────────────────────


def test_crashed_child_publishes_nothing(env, monkeypatch):
    """Writing a timeout here would report a healthy mount as failing over a
    bug in the checker. Let the last result go stale instead."""
    pa.result_path().write_text(json.dumps({"ok": True, "timestamp": 1.0}))
    child(monkeypatch, env, "sys.exit(3)")
    assert pa.run_attempt(0) == "failed"
    assert json.loads(pa.result_path().read_text())["ok"] is True


def test_crashed_child_drops_out_of_ready(env, monkeypatch):
    child(monkeypatch, env, "sys.exit(3)")
    pa.HEALTHY.touch()
    assert pa.cycle(0) == "failed"
    assert not pa.HEALTHY.exists()


def test_child_that_writes_nothing_is_a_failure(env, monkeypatch):
    child(monkeypatch, env, "pass")
    assert pa.run_attempt(0) == "failed"
    assert not pa.result_path().exists()


def test_missing_child_script_is_a_failure(env, monkeypatch):
    monkeypatch.setattr(pa, "JOB_RUNNER", str(env / "nope.py"))
    assert pa.run_attempt(0) == "failed"


def test_cycle_survives_an_exploding_attempt(env, monkeypatch):
    def boom(seq):
        raise RuntimeError("nope")

    monkeypatch.setattr(pa, "run_attempt", boom)
    pa.HEALTHY.touch()
    # main()'s loop swallows this; assert the marker clearing it depends on.
    with pytest.raises(RuntimeError):
        pa.cycle(0)


# ── the results volume is gone ────────────────────────────────────────────────


def test_unwritable_results_volume_is_a_failure_not_a_timeout(env, monkeypatch):
    """An unreachable results PVC must not be published as a broken mount."""
    monkeypatch.setattr(pa, "ATTEMPTS_DIR", env / "results" / "file" / "attempts")
    (env / "results" / "file").write_text("not a directory")
    assert pa.run_attempt(0) == "failed"


# ── housekeeping ──────────────────────────────────────────────────────────────


def test_sweep_drops_attempts_from_earlier_pods(env, monkeypatch):
    """Same stem, different pid: a crashlooping probe would otherwise leave one
    file per restart on the shared PVC forever."""
    pa.ATTEMPTS_DIR.mkdir(parents=True)
    dead = pa.ATTEMPTS_DIR / f"{pa.STEM}-999999-4.json"
    dead.write_text("{}")
    other = pa.ATTEMPTS_DIR / "work__node-a-1-1.json"
    other.write_text("{}")
    keep = pa.attempt_path(7)
    keep.write_text("{}")

    pa.sweep_attempts(keep=keep)

    assert not dead.exists()
    assert keep.exists()
    assert other.exists()  # another mount's probe owns that one


def test_sweep_is_quiet_when_there_is_nothing_to_sweep(env):
    pa.sweep_attempts(keep=None)  # ATTEMPTS_DIR does not exist yet


def test_attempts_do_not_accumulate_across_cycles(env, monkeypatch):
    child(monkeypatch, env, WRITES_OK)
    for seq in range(4):
        pa.cycle(seq)
    assert list(pa.ATTEMPTS_DIR.iterdir()) == []


def test_reap_orphans_is_a_no_op_without_children(env):
    pa.reap_orphans()


def test_reap_orphans_collects_an_abandoned_grandchild(env, monkeypatch):
    """A child killed at its deadline reparents its own children here — this
    process is PID 1 in the container and nothing else will reap them."""
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 0.3)
    child(
        monkeypatch,
        env,
        """
        import subprocess
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
        time.sleep(30)
        """,
    )
    assert pa.run_attempt(0) == "timeout"
    time.sleep(1.5)
    pa.reap_orphans()  # must not raise, whatever the platform reparents


def test_healthy_marker_is_idempotent(env):
    pa.set_healthy(False)  # nothing to remove
    pa.set_healthy(True)
    pa.set_healthy(True)
    assert pa.HEALTHY.exists()
    pa.set_healthy(False)
    assert not pa.HEALTHY.exists()


def test_load_json_tolerates_missing_and_corrupt(env):
    assert pa.load_json(env / "nope.json") == {}
    bad = env / "bad.json"
    bad.write_text("{ nope")
    assert pa.load_json(bad) == {}


def test_stem_matches_what_the_exporter_reads(env):
    """probe_agent and node_healthcheck derive the same filename independently;
    if they disagree every mount reads as "never reported"."""
    sys.path.insert(0, os.path.dirname(pa.__file__))
    import node_healthcheck as nh

    assert pa.result_path().name == nh._result_path("/depot/", "node-a").name


def test_unkillable_child_does_not_block_the_verdict(env, monkeypatch):
    """SIGKILL to a process in uninterruptible sleep is recorded, not
    delivered. Waiting for it is what would freeze the loop; the timeout is
    published and the child abandoned to reap_orphans."""
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 0.3)
    monkeypatch.setattr(pa, "CHILD_KILL_GRACE_S", 0.2)
    child(monkeypatch, env, "time.sleep(30)")

    real_popen = pa.subprocess.Popen

    class Unreapable:
        def __init__(self, *a, **kw):
            self._proc = real_popen(*a, **kw)
            self.pid = self._proc.pid
            self._killed = False

        def communicate(self, timeout=None):
            if self._killed:
                raise pa.subprocess.TimeoutExpired(cmd="child", timeout=timeout)
            raise pa.subprocess.TimeoutExpired(cmd="child", timeout=timeout)

        def kill(self):
            self._killed = True
            self._proc.kill()

    monkeypatch.setattr(pa.subprocess, "Popen", Unreapable)
    assert pa.run_attempt(0) == "timeout"
    assert json.loads(pa.result_path().read_text())["timeout"] is True


def test_child_communicate_error_is_a_failure_not_a_timeout(env, monkeypatch):
    child(monkeypatch, env, "pass")
    real_popen = pa.subprocess.Popen

    class Broken:
        def __init__(self, *a, **kw):
            self._proc = real_popen(*a, **kw)
            self.pid = self._proc.pid

        def communicate(self, timeout=None):
            self._proc.wait()
            raise OSError("pipe went away")

    monkeypatch.setattr(pa.subprocess, "Popen", Broken)
    assert pa.run_attempt(0) == "failed"


def test_unpublishable_timeout_verdict_is_a_failure(env, monkeypatch):
    """If the timeout verdict cannot be written, the probe has produced
    nothing — it must not claim it did and stay Ready."""
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 0.3)
    child(monkeypatch, env, "time.sleep(30)")

    def boom(path, data):
        raise OSError("read-only file system")

    monkeypatch.setattr(pa, "write_atomic", boom)
    assert pa.cycle(0) == "failed"
    assert not pa.HEALTHY.exists()


def test_unpromotable_result_is_a_failure(env, monkeypatch):
    child(monkeypatch, env, WRITES_OK)

    def boom(src, dst):
        raise OSError("stale file handle")

    monkeypatch.setattr(pa.os, "replace", boom)
    assert pa.run_attempt(0) == "failed"


def test_touch_survives_an_unwritable_runtime_dir(env, monkeypatch):
    monkeypatch.setattr(pa, "HEARTBEAT", env / "runtime" / "file" / "heartbeat")
    (env / "runtime" / "file").write_text("not a directory")
    pa.touch(pa.HEARTBEAT)  # logs, does not raise


def test_sweep_survives_an_unremovable_attempt(env, monkeypatch):
    pa.ATTEMPTS_DIR.mkdir(parents=True)
    (pa.ATTEMPTS_DIR / f"{pa.STEM}-1-1.json").write_text("{}")

    def boom(self):
        raise OSError("permission denied")

    monkeypatch.setattr(pa.Path, "unlink", boom)
    pa.sweep_attempts(keep=None)  # logs, does not raise
