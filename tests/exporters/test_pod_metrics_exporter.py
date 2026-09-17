"""Tests for docker/af-pod-monitor/pod-metrics-exporter.py."""

import pytest
from exporter_helpers import pod_exporter
from prometheus_client import REGISTRY

exporter = pod_exporter()

DF_OUTPUT = """\
Filesystem     1K-blocks    Used Available Use% Mounted on
storage         26214400 5242880  20971520  20% /home/alice
"""

DF_OUTPUT_SIZE_HEADER = """\
Filesystem     Size    Used Available Use% Mounted on
storage        26214400 5242880  20971520  20% /home/alice
"""

DU_OUTPUT = "52428800\t/work/users/alice/\n"
# The same 50 GiB as DU_OUTPUT, as CephFS reports it: bytes, not KB.
RBYTES = f"{52428800 * 1024}\n"


def gauge_value(name):
    return REGISTRY.get_sample_value(name)


@pytest.fixture(autouse=True)
def forget_failures():
    """update_directory logs on transition, so each test starts from healthy."""
    exporter._failing.clear()


def answers(**by_command):
    """A run_bounded whose reply is chosen by the command being run."""

    def run_bounded(cmd, _timeout):
        for name, reply in by_command.items():
            if name == "rbytes" and "-c" in cmd:
                return reply
            if cmd[0] == name:
                return reply
        raise AssertionError(f"unexpected command: {cmd}")

    return run_bounded


# ── parsing ───────────────────────────────────────────────────────────────────


def test_parse_df_1k_blocks():
    used, size, util = exporter.parse_df_output(DF_OUTPUT)
    assert used == 5242880
    assert size == 26214400
    assert util == pytest.approx(0.2)


def test_parse_df_size_header():
    used, size, util = exporter.parse_df_output(DF_OUTPUT_SIZE_HEADER)
    assert size == 26214400
    assert util == pytest.approx(0.2)


def test_parse_du_uses_fixed_quota():
    used, size, util = exporter.parse_du_output(DU_OUTPUT)
    assert used == 52428800
    assert size == exporter.WORK_QUOTA_KB
    assert util == pytest.approx(0.5)


# ── discovery ─────────────────────────────────────────────────────────────────


def test_discover_username_skips_system_accounts():
    assert exporter.discover_username(["jovyan", "slurm", "alice"]) == "alice"


def test_discover_username_no_user_raises():
    with pytest.raises(StopIteration):
        exporter.discover_username(["jovyan", "slurm"])


def test_discover_directories_uses_the_users_home(monkeypatch):
    """Regression: home was the first /home glob hit, which could be jovyan
    while /work was already resolved to the real user."""
    monkeypatch.setattr(exporter.os, "listdir", lambda path: ["jovyan", "alice"])
    assert exporter.discover_directories() == {
        "home": "/home/alice",
        "work": "/work/users/alice/",
    }


# ── update_metrics writes to the right gauges ─────────────────────────────────


def test_update_metrics_home(monkeypatch, tmp_path):
    monkeypatch.setattr(exporter, "run_bounded", lambda cmd, timeout: (True, DF_OUTPUT))

    exporter.update_metrics("home", str(tmp_path))

    assert gauge_value("af_home_dir_used_kb") == 5242880
    assert gauge_value("af_home_dir_size_kb") == 26214400
    assert gauge_value("af_home_dir_util") == pytest.approx(0.2)


def test_update_metrics_work_does_not_touch_home(monkeypatch, tmp_path):
    """Regression: the old code wrote every reading into the same gauges."""
    monkeypatch.setattr(exporter, "run_bounded", lambda cmd, timeout: (True, DF_OUTPUT))
    exporter.update_metrics("home", str(tmp_path))

    monkeypatch.setattr(exporter, "run_bounded", answers(rbytes=(True, RBYTES)))
    exporter.update_metrics("work", str(tmp_path))

    assert gauge_value("af_work_dir_used_kb") == 52428800
    assert gauge_value("af_work_dir_size_kb") == exporter.WORK_QUOTA_KB
    # home gauges keep their own values
    assert gauge_value("af_home_dir_used_kb") == 5242880


# ── one unreadable directory must not stop the others ─────────────────────────


def fail(_cmd, _timeout):
    return False, ""


def test_update_directory_reports_success(monkeypatch):
    monkeypatch.setattr(exporter, "run_bounded", lambda cmd, timeout: (True, DF_OUTPUT))

    assert exporter.update_directory("home", "/home/alice") is True
    assert gauge_value("af_home_dir_ok") == 1


def test_update_directory_swallows_an_unreadable_mount(monkeypatch):
    """`du: cannot access '/work/users/<user>/': Permission denied` used to
    raise out of the loop and kill the sidecar — 275 restarts on one pod."""
    monkeypatch.setattr(exporter, "run_bounded", fail)

    assert exporter.update_directory("work", "/work/users/alice/") is False
    assert gauge_value("af_work_dir_ok") == 0


def test_a_failing_work_dir_leaves_home_metrics_alone(monkeypatch):
    """/home utilisation is what the quota alerts fire on, so it has to survive
    /work being unreadable."""
    monkeypatch.setattr(exporter, "run_bounded", lambda cmd, timeout: (True, DF_OUTPUT))
    exporter.update_directory("home", "/home/alice")

    monkeypatch.setattr(exporter, "run_bounded", fail)
    exporter.update_directory("work", "/work/users/alice/")

    assert gauge_value("af_home_dir_used_kb") == 5242880
    assert gauge_value("af_home_dir_util") == pytest.approx(0.2)
    assert gauge_value("af_home_dir_ok") == 1
    assert gauge_value("af_work_dir_ok") == 0


def test_an_unreadable_home_is_flagged(monkeypatch):
    monkeypatch.setattr(exporter, "run_bounded", fail)

    assert exporter.update_directory("home", "/home/alice") is False
    assert gauge_value("af_home_dir_ok") == 0


def test_a_persistent_fault_is_logged_once_and_on_recovery(monkeypatch, caplog):
    """`du` timing out every pass logged the same traceback every 15 minutes
    on 11 pods; af_work_dir_ok already carried the state continuously."""
    monkeypatch.setattr(exporter, "run_bounded", fail)

    with caplog.at_level("INFO", logger="af-pod-monitor"):
        for _ in range(4):
            exporter.update_directory("work", "/work/users/alice/")
        assert len(caplog.records) == 1
        assert gauge_value("af_work_dir_ok") == 0

        caplog.clear()
        monkeypatch.setattr(exporter, "run_bounded", answers(rbytes=(True, "1024\n")))
        exporter.update_directory("work", "/work/users/alice/")

    assert gauge_value("af_work_dir_ok") == 1
    assert [record.getMessage() for record in caplog.records] == [
        "work directory (/work/users/alice/) is readable again"
    ]


def test_a_fault_that_clears_and_returns_is_logged_again(monkeypatch, caplog):
    monkeypatch.setattr(exporter, "run_bounded", fail)
    with caplog.at_level("ERROR", logger="af-pod-monitor"):
        exporter.update_directory("work", "/work/users/alice/")
        monkeypatch.setattr(exporter, "run_bounded", answers(rbytes=(True, "1024\n")))
        exporter.update_directory("work", "/work/users/alice/")
        monkeypatch.setattr(exporter, "run_bounded", fail)
        exporter.update_directory("work", "/work/users/alice/")
    assert len(caplog.records) == 2


# ── /work is read, not walked ─────────────────────────────────────────────────


def test_work_usage_comes_from_the_ceph_recursive_byte_count(monkeypatch):
    """One xattr instead of a walk: `du -s` over a two-million-file tree ran
    past DU_TIMEOUT_S on every pass, leaving those users with no /work
    metrics at all."""
    monkeypatch.setattr(
        exporter, "run_bounded", answers(rbytes=(True, "399368164695\n"))
    )

    used, size, util = exporter.read_work_usage("/work/users/alice/")
    assert used == 399368164695 // 1024
    assert size == exporter.WORK_QUOTA_KB
    assert util == pytest.approx(used / exporter.WORK_QUOTA_KB)


def test_a_mount_without_the_xattr_is_still_walked(monkeypatch):
    """The xattr is CephFS's; anything else falls back to `du -s`."""
    monkeypatch.setattr(
        exporter, "run_bounded", answers(rbytes=(True, ""), du=(True, DU_OUTPUT))
    )

    assert exporter.read_work_usage("/work/users/alice/") == (
        52428800,
        exporter.WORK_QUOTA_KB,
        pytest.approx(0.5),
    )


def test_an_unanswered_xattr_is_not_then_handed_to_du(monkeypatch):
    """A mount that did not answer a statfs-speed read in RBYTES_TIMEOUT_S
    will not answer a walk either — it would only hang DU_TIMEOUT_S longer."""
    commands = []

    def run_bounded(cmd, _timeout):
        commands.append(cmd[0])
        return False, ""

    monkeypatch.setattr(exporter, "run_bounded", run_bounded)

    with pytest.raises(OSError):
        exporter.read_work_usage("/work/users/alice/")
    assert "du" not in commands


def test_the_xattr_read_is_bounded_like_every_other_command(monkeypatch):
    """An xattr on a dead mount hangs exactly as a walk does, so it runs in a
    child with a timeout rather than in the loop."""
    seen = {}

    def run_bounded(cmd, timeout):
        seen["cmd"], seen["timeout"] = cmd, timeout
        return True, "1024\n"

    monkeypatch.setattr(exporter, "run_bounded", run_bounded)
    exporter.read_work_usage("/work/users/alice/")

    assert seen["timeout"] == exporter.RBYTES_TIMEOUT_S
    assert seen["cmd"][:2] == [exporter.sys.executable, "-c"]
    assert seen["cmd"][-1] == "/work/users/alice/"


# ── session responsiveness ────────────────────────────────────────────────────


class _HungProc:
    """A child parked on a dead mount: communicate() never returns on its own."""

    returncode = None

    def __init__(self):
        self.killed = False

    def communicate(self, timeout=None):
        if self.killed:
            return "", ""
        raise exporter.subprocess.TimeoutExpired(cmd="ls", timeout=timeout)

    def kill(self):
        self.killed = True


def test_a_readable_session_is_responsive(tmp_path):
    assert exporter.probe_session(str(tmp_path)) is True
    assert gauge_value("af_session_responsive") == 1
    assert gauge_value("af_session_probe_seconds") >= 0


def test_a_hung_session_is_not_waited_out(monkeypatch):
    """The failure this exists for: an unbounded read on a wedged CephFS mount
    parks the whole pass, and the HTTP server keeps serving the last good
    values — a session nobody can work in, reported as healthy."""
    hung = _HungProc()
    monkeypatch.setattr(exporter.subprocess, "Popen", lambda *a, **k: hung)
    before = gauge_value("af_session_probe_failures_total") or 0

    assert exporter.probe_session("/home/alice") is False
    assert hung.killed, "a child stuck on a dead mount must be abandoned"
    assert gauge_value("af_session_responsive") == 0
    assert gauge_value("af_session_probe_failures_total") == before + 1


def test_run_bounded_reports_a_failed_command():
    ok, out = exporter.run_bounded(["false"], 5)
    assert ok is False and out == ""


def test_run_bounded_returns_stdout():
    assert exporter.run_bounded(["echo", "hi"], 5) == (True, "hi\n")


def test_run_bounded_survives_a_missing_binary():
    assert exporter.run_bounded(["definitely-not-a-binary"], 5) == (False, "")


def test_run_bounded_abandons_a_child_that_ignores_kill(monkeypatch):
    """SIGKILL is not delivered while the child sits in a syscall on a dead
    mount; waiting for it would park the loop all the same."""

    class Unkillable(_HungProc):
        def communicate(self, timeout=None):
            raise exporter.subprocess.TimeoutExpired(cmd="df", timeout=timeout)

    monkeypatch.setattr(exporter.subprocess, "Popen", lambda *a, **k: Unkillable())
    assert exporter.run_bounded(["df", "/home/alice"], 5) == (False, "")


def test_usage_commands_are_bounded(monkeypatch):
    """`df` on a dead mount blocks forever without a bound, and subprocess's
    own timeout waits for the child it just killed. The xattr read and the
    `du` fallback behind it are the same kind of call and carry the same
    protection."""
    calls = []

    def record(cmd, timeout):
        name = "rbytes" if "-c" in cmd else cmd[0]
        calls.append((name, timeout))
        return True, {"df": DF_OUTPUT, "du": DU_OUTPUT, "rbytes": ""}[name]

    monkeypatch.setattr(exporter, "run_bounded", record)
    exporter.update_metrics("home", "/home/alice")
    exporter.update_metrics("work", "/work/users/alice/")

    assert calls == [
        ("df", exporter.DF_TIMEOUT_S),
        ("rbytes", exporter.RBYTES_TIMEOUT_S),
        ("du", exporter.DU_TIMEOUT_S),
    ]


def test_heartbeat_starts_at_a_real_time():
    """A gauge starts at 0, which reads as 1970 to anything asking how long
    ago the last pass was: a session that had only just started looked stale,
    and the dashboard called it unresponsive until its first pass finished."""
    exporter.start_heartbeat()
    assert gauge_value("af_pod_monitor_last_pass_timestamp_seconds") > 1e9


# ── main loop ─────────────────────────────────────────────────────────────────


class _StopLoop(Exception):
    pass


def run_main(monkeypatch, probes):
    """Run main() for len(probes) passes; return the directories updated and
    the heartbeat seen at the end of each pass."""
    probes = list(probes)
    updated = []
    beats = []
    monkeypatch.setattr(
        exporter,
        "discover_directories",
        lambda: {"home": "/home/alice", "work": "/work/users/alice/"},
    )
    monkeypatch.setattr(exporter, "start_http_server", lambda port: None)
    monkeypatch.setattr(exporter, "start_heartbeat", lambda: None)
    monkeypatch.setattr(exporter.logging, "basicConfig", lambda **kw: None)
    monkeypatch.setattr(exporter, "probe_session", lambda home: probes.pop(0))
    monkeypatch.setattr(
        exporter, "update_directory", lambda label, d: updated.append(label)
    )

    def sleep(seconds):
        assert seconds == exporter.INTERVAL
        beats.append(gauge_value("af_pod_monitor_last_pass_timestamp_seconds"))
        exporter.heartbeat.set(0)
        if not probes:
            raise _StopLoop

    monkeypatch.setattr(exporter.time, "sleep", sleep)
    with pytest.raises(_StopLoop):
        exporter.main()
    return updated, beats


def test_main_reads_every_directory_each_pass(monkeypatch):
    updated, _ = run_main(monkeypatch, [True, True])
    assert updated == ["home", "work"] * 2


def test_main_skips_usage_on_an_unresponsive_session(monkeypatch):
    """Reading usage touches the mount that just failed to answer; the
    heartbeat still advances so the wedge is visible."""
    exporter.heartbeat.set(0)
    updated, beats = run_main(monkeypatch, [False, True])
    assert updated == ["home", "work"]  # the second pass only
    assert all(beat > 1e9 for beat in beats)
