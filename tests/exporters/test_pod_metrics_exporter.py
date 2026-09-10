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


def gauge_value(name):
    return REGISTRY.get_sample_value(name)


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

    monkeypatch.setattr(exporter, "run_bounded", lambda cmd, timeout: (True, DU_OUTPUT))
    exporter.update_metrics("work", str(tmp_path))

    assert gauge_value("af_work_dir_used_kb") == 52428800
    assert gauge_value("af_work_dir_size_kb") == exporter.WORK_QUOTA_KB
    # home gauges keep their own values
    assert gauge_value("af_home_dir_used_kb") == 5242880


# ── one unreadable directory must not stop the others ─────────────────────────


def fail(_cmd, _timeout=None):
    raise exporter.subprocess.CalledProcessError(1, "du")


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


def test_last_accessed_gauges_are_gone():
    """st_atime measured the mount, not the user: frozen for years on some
    homes, and on /work only ever the exporter's own `du` walk."""
    assert gauge_value("af_home_dir_last_accessed") is None
    assert gauge_value("af_work_dir_last_accessed") is None


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


def counter_value(name):
    return REGISTRY.get_sample_value(name)


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
    before = counter_value("af_session_probe_failures_total") or 0

    assert exporter.probe_session("/home/alice") is False
    assert hung.killed, "a child stuck on a dead mount must be abandoned"
    assert gauge_value("af_session_responsive") == 0
    assert counter_value("af_session_probe_failures_total") == before + 1


def test_run_bounded_reports_a_failed_command(monkeypatch):
    ok, out = exporter.run_bounded(["false"], 5)
    assert ok is False and out == ""


def test_usage_commands_are_bounded():
    """`df` on a dead mount blocks forever without a bound, and subprocess's
    own timeout waits for the child it just killed."""
    assert exporter.DF_TIMEOUT_S > 0
    assert exporter.DU_TIMEOUT_S > exporter.DF_TIMEOUT_S


def test_heartbeat_gauge_exists():
    """Every gauge can sit at its last value while the loop is wedged; only
    the heartbeat shows that no pass has finished."""
    assert exporter.heartbeat is not None
