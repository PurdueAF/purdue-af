"""Tests for docker/af-node-monitor/probe_agent.py.

The supervisor keeps three failures apart:

  the mount did not answer   -> publish a timeout verdict (goes red)
  the probe itself broke     -> publish nothing, drop out of Ready (goes stale)
  the probe loop stopped     -> the heartbeat stops (the kubelet's checks)

A child is a real subprocess here rather than a mock: the deadline path exists
because a check can be unkillable, and that only shows up across a fork.
"""

import json
import os
import socket
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request

os.environ.setdefault("MOUNT_NAME", "/depot/")
os.environ.setdefault("NODE_NAME", "node-a")
os.environ.setdefault("PROBE_STARTUP_JITTER_S", "0")

import probe_agent as pa  # noqa: E402
import pytest  # noqa: E402
from common import mount_configmap  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Point every module-level path at tmp_path; they are import-time derived."""
    runtime = tmp_path / "runtime"
    results = runtime / "results"
    results.mkdir(parents=True)
    monkeypatch.setattr(pa, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(pa, "RESULTS_DIR", results)
    monkeypatch.setattr(pa, "ATTEMPTS_DIR", results / ".attempts")
    monkeypatch.setattr(pa, "HEARTBEAT", runtime / "heartbeat")
    monkeypatch.setattr(pa, "READY", runtime / "ready")
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 5.0)
    monkeypatch.setattr(pa, "PUBLISHED", pa.Published())
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


def served():
    body = pa.PUBLISHED.get()
    return None if body is None else json.loads(body)


# ── the happy path ────────────────────────────────────────────────────────────


def test_successful_attempt_is_published(env, monkeypatch):
    child(monkeypatch, env, WRITES_OK)
    assert pa.run_attempt(0) == "published"
    assert json.loads(pa.result_path().read_text())["ok"] is True
    assert served()["ok"] is True


def test_cycle_marks_ready_and_beats(env, monkeypatch):
    child(monkeypatch, env, WRITES_OK)
    assert pa.cycle(0) == "published"
    assert pa.READY.exists()
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
    assert served()["seen"] == 1234.0


# ── the mount did not answer ──────────────────────────────────────────────────


def test_deadline_publishes_a_timeout_verdict(env, monkeypatch):
    """A check that outlives its deadline still yields a timeout verdict."""
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 0.3)
    child(monkeypatch, env, "time.sleep(30)")
    assert pa.run_attempt(0) == "timeout"

    published = served()
    assert published == json.loads(pa.result_path().read_text())
    assert published["ok"] is False
    assert published["timeout"] is True
    assert published["throughput_gbps"] is None
    assert published["node"] == "node-a"
    # Null, not a partial reading off a wedged mount — the exporter substitutes
    # its own timeout sentinels.
    assert published["ping_ms"] is None
    assert published["metadata_ms"] is None


def test_timeout_carries_the_fio_history_forward(env, monkeypatch):
    """A recovering mount must neither run fio on every cycle nor report a
    zero rate it never measured."""
    pa.result_path().write_text(
        json.dumps({"last_fio_ts": 999.0, "throughput_gbps": 6.4})
    )
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 0.3)
    child(monkeypatch, env, "time.sleep(30)")
    pa.run_attempt(0)
    assert served()["last_fio_ts"] == 999.0
    assert served()["throughput_gbps"] == 6.4


def test_a_timed_out_probe_stays_ready(env, monkeypatch):
    """It published a verdict, so the probe works. Dropping out of Ready here
    would say "monitoring is broken" about a working probe on a dead mount."""
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 0.3)
    child(monkeypatch, env, "time.sleep(30)")
    assert pa.cycle(0) == "timeout"
    assert pa.READY.exists()


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
    assert "stale" not in served()


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

        def communicate(self, timeout=None):
            raise pa.subprocess.TimeoutExpired(cmd="child", timeout=timeout)

        def kill(self):
            self._proc.kill()

    monkeypatch.setattr(pa.subprocess, "Popen", Unreapable)
    assert pa.run_attempt(0) == "timeout"
    assert served()["timeout"] is True


# ── the probe itself broke ────────────────────────────────────────────────────


def test_crashed_child_publishes_nothing(env, monkeypatch):
    """Writing a timeout here would report a healthy mount as failing over a
    bug in the checker. Let the last result go stale instead."""
    pa.PUBLISHED.set({"ok": True, "timestamp": 1.0})
    child(monkeypatch, env, "sys.exit(3)")
    assert pa.run_attempt(0) == "failed"
    assert served() == {"ok": True, "timestamp": 1.0}


def test_crashed_child_drops_out_of_ready(env, monkeypatch):
    child(monkeypatch, env, WRITES_OK)
    pa.cycle(0)
    assert pa.READY.exists()

    child(monkeypatch, env, "sys.exit(3)")
    assert pa.cycle(1) == "failed"
    assert not pa.READY.exists()


def test_child_that_writes_nothing_is_a_failure(env, monkeypatch):
    child(monkeypatch, env, "pass")
    assert pa.run_attempt(0) == "failed"
    assert not pa.result_path().exists()
    assert served() is None


def test_missing_child_script_is_a_failure(env, monkeypatch):
    monkeypatch.setattr(pa, "JOB_RUNNER", str(env / "nope.py"))
    assert pa.run_attempt(0) == "failed"


def test_child_that_cannot_start_is_a_failure(env, monkeypatch):
    def boom(*a, **kw):
        raise OSError("exec format error")

    monkeypatch.setattr(pa.subprocess, "Popen", boom)
    assert pa.run_attempt(0) == "failed"


def test_cycle_survives_an_exploding_attempt(env, monkeypatch):
    """A crashed probe must stop advertising itself as Ready. Leaving that to
    main() would make the invariant depend on the caller."""

    def boom(seq):
        raise RuntimeError("nope")

    monkeypatch.setattr(pa, "run_attempt", boom)
    pa.touch(pa.READY)
    assert pa.cycle(0) == "failed"
    assert not pa.READY.exists()
    assert pa.HEARTBEAT.exists()


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


def test_unrecordable_timeout_verdict_is_a_failure(env, monkeypatch):
    """If the timeout verdict cannot be kept, the probe has produced nothing —
    it must not claim it did and stay Ready."""
    monkeypatch.setattr(pa, "PROBE_DEADLINE_S", 0.3)
    child(monkeypatch, env, "time.sleep(30)")

    def boom(path, data):
        raise OSError("read-only file system")

    monkeypatch.setattr(pa, "write_atomic", boom)
    assert pa.cycle(0) == "failed"
    assert not pa.READY.exists()
    assert served() is None


def test_unpromotable_result_is_a_failure(env, monkeypatch):
    child(monkeypatch, env, WRITES_OK)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(pa.os, "replace", boom)
    assert pa.run_attempt(0) == "failed"
    assert served() is None


def test_unwritable_results_dir_is_a_failure_not_a_timeout(env, monkeypatch):
    monkeypatch.setattr(pa, "ATTEMPTS_DIR", pa.RESULTS_DIR / "file" / "attempts")
    (pa.RESULTS_DIR / "file").write_text("not a directory")
    assert pa.run_attempt(0) == "failed"


# ── a restarted container ─────────────────────────────────────────────────────


def test_a_restarted_container_serves_its_last_verdict_but_is_not_ready(env):
    """The emptyDir outlives a container restart, so the verdict survives it;
    the Ready marker does not carry over, since this container has not yet
    produced a verdict of its own."""
    pa.result_path().write_text(json.dumps({"ok": False, "timeout": True}))
    pa.touch(pa.READY)
    pa.start()
    assert served() == {"ok": False, "timeout": True}
    assert not pa.READY.exists()
    assert pa.HEARTBEAT.exists()


def test_start_without_a_previous_result_serves_nothing(env):
    pa.start()
    assert served() is None


def test_only_new_probe_code_ends_the_probe_and_only_between_cycles(env, monkeypatch):
    """The cycle new code lands in runs to its end and no other starts after
    it; job_runner.py runs afresh every cycle."""
    scripts = env / "scripts"
    scripts.mkdir()
    files = dict.fromkeys(
        ("probe_agent.py", "job_runner.py", "node_healthcheck.py"), ""
    )
    mount_configmap(scripts, 0, files)
    versions = [
        {**files, "job_runner.py": "new"},
        {**files, "job_runner.py": "new", "probe_agent.py": "new"},
    ]
    cycles = []

    def cycle(seq):
        if seq < len(versions):
            mount_configmap(scripts, seq + 1, versions[seq])
        cycles.append(seq)
        return "published"

    monkeypatch.setattr(pa, "CODE", scripts / "probe_agent.py")
    monkeypatch.setattr(pa, "serve", lambda: None)
    monkeypatch.setattr(pa, "cycle", cycle)
    monkeypatch.setattr(pa, "PROBE_INTERVAL_S", 0.0)
    monkeypatch.setattr(pa, "PROBE_STARTUP_JITTER_S", 0.0)
    pa.main()
    assert cycles == [0, 1]


# ── the HTTP endpoint the exporter reads ──────────────────────────────────────


@pytest.fixture
def server(env):
    srv = pa.serve(port=0)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_http_serves_the_published_result(server):
    assert get(f"{server}/result")[0] == 404
    pa.PUBLISHED.set({"ok": True, "timestamp": 5.0})
    status, body = get(f"{server}/result")
    assert status == 200
    assert json.loads(body) == {"ok": True, "timestamp": 5.0}


def test_http_serves_nothing_else(server):
    pa.PUBLISHED.set({"ok": True})
    assert get(f"{server}/metrics")[0] == 404


@pytest.mark.parametrize("trickle", [False, True])
def test_a_slow_client_cannot_hold_the_server(server, monkeypatch, trickle):
    """One serving thread: a client that never finishes its request, whether it
    sends nothing or a byte at a time, is cut off at the deadline."""
    monkeypatch.setattr(pa, "HTTP_TIMEOUT_S", 0.5)
    pa.PUBLISHED.set({"ok": True})
    host, port = server.removeprefix("http://").split(":")
    slow = socket.create_connection((host, int(port)))
    stop = threading.Event()

    def drip():
        for byte in b"GET /result HTTP/1.1\r\nX-Slow: " + b"a" * 1000:
            if stop.is_set():
                return
            try:
                slow.send(bytes([byte]))
            except OSError:
                return
            time.sleep(0.1)

    if trickle:
        threading.Thread(target=drip, daemon=True).start()
    try:
        started = time.time()
        assert get(f"{server}/result", timeout=5)[0] == 200
        assert time.time() - started < 3
    finally:
        stop.set()
        slow.close()


def test_the_exporter_reads_what_the_probe_serves(server):
    """probe_agent and node_healthcheck meet only over HTTP; if they disagree on
    the path or the encoding, every mount reads as unknown."""
    sys.path.insert(0, os.path.dirname(pa.__file__))
    import node_healthcheck as nh

    pa.PUBLISHED.set({"ok": True, "timestamp": 7.0, "node": "node-a"})
    port = int(server.rsplit(":", 1)[1])
    assert nh._fetch_result("127.0.0.1", port) == {
        "ok": True,
        "timestamp": 7.0,
        "node": "node-a",
    }


# ── housekeeping ──────────────────────────────────────────────────────────────


def test_readiness_clear_survives_an_unwritable_runtime_dir(env, monkeypatch, capsys):
    pa.touch(pa.READY)

    def boom(self):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(pa.Path, "unlink", boom)
    pa.set_ready(False)  # logs, does not raise
    assert "cannot clear" in capsys.readouterr().err


def test_touch_survives_an_unwritable_runtime_dir(env):
    blocker = env / "runtime" / "file"
    blocker.write_text("not a directory")
    pa.touch(blocker / "heartbeat")  # logs, does not raise


def test_sweep_drops_attempts_from_earlier_containers(env, monkeypatch):
    """Different pid: a crashlooping probe would otherwise leave one file per
    restart on the emptyDir."""
    pa.ATTEMPTS_DIR.mkdir(parents=True)
    dead = pa.ATTEMPTS_DIR / "999999-4.json"
    dead.write_text("{}")
    keep = pa.attempt_path(7)
    keep.write_text("{}")

    pa.sweep_attempts(keep=keep)

    assert not dead.exists()
    assert keep.exists()


def test_sweep_is_quiet_when_there_is_nothing_to_sweep(env):
    pa.sweep_attempts(keep=None)  # ATTEMPTS_DIR does not exist yet


def test_attempts_do_not_accumulate_across_cycles(env, monkeypatch):
    child(monkeypatch, env, WRITES_OK)
    for seq in range(4):
        pa.cycle(seq)
    assert list(pa.ATTEMPTS_DIR.iterdir()) == []


def test_sweep_survives_an_unremovable_attempt(env, monkeypatch):
    pa.ATTEMPTS_DIR.mkdir(parents=True)
    (pa.ATTEMPTS_DIR / "1-1.json").write_text("{}")

    def boom(self):
        raise OSError("permission denied")

    monkeypatch.setattr(pa.Path, "unlink", boom)
    pa.sweep_attempts(keep=None)  # logs, does not raise


def test_sweep_tolerates_an_attempt_that_vanished(env, monkeypatch, capsys):
    pa.ATTEMPTS_DIR.mkdir(parents=True)
    (pa.ATTEMPTS_DIR / "1-1.json").write_text("{}")

    def gone(self):
        raise FileNotFoundError(self)

    monkeypatch.setattr(pa.Path, "unlink", gone)
    pa.sweep_attempts(keep=None)
    assert capsys.readouterr().err == ""


def test_reap_orphans_is_a_no_op_without_children(env):
    pa.reap_orphans()


def test_reap_orphans_collects_an_abandoned_child(env):
    """Children abandoned at their deadline, and grandchildren reparented to
    this process (PID 1 in the container), are reaped nowhere else. A child
    still running is left alone rather than waited on."""
    live = pa.subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    dead = pa.subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.time() + 10
    while not _is_zombie(dead.pid) and time.time() < deadline:
        time.sleep(0.05)

    try:
        pa.reap_orphans()

        with pytest.raises(ChildProcessError):
            os.waitpid(dead.pid, os.WNOHANG)
        dead.returncode = 0  # already reaped; keep Popen.__del__ quiet
        assert live.poll() is None
    finally:
        live.kill()
        live.wait()


def _is_zombie(pid):
    state = pa.subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout
    return state.startswith("Z")


def test_load_json_tolerates_missing_and_corrupt(env):
    assert pa.load_json(env / "nope.json") == {}
    bad = env / "bad.json"
    bad.write_text("{ nope")
    assert pa.load_json(bad) == {}


def test_mount_name_is_required(monkeypatch):
    monkeypatch.delenv("MOUNT_NAME", raising=False)
    with pytest.raises(RuntimeError, match="MOUNT_NAME"):
        pa._get_env("MOUNT_NAME", required=True)
