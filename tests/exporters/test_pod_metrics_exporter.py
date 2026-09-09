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
    monkeypatch.setattr(
        exporter.subprocess, "check_output", lambda cmd: DF_OUTPUT.encode()
    )

    exporter.update_metrics("home", str(tmp_path))

    assert gauge_value("af_home_dir_used_kb") == 5242880
    assert gauge_value("af_home_dir_size_kb") == 26214400
    assert gauge_value("af_home_dir_util") == pytest.approx(0.2)


def test_update_metrics_work_does_not_touch_home(monkeypatch, tmp_path):
    """Regression: the old code wrote every reading into the same gauges."""
    monkeypatch.setattr(
        exporter.subprocess, "check_output", lambda cmd: DF_OUTPUT.encode()
    )
    exporter.update_metrics("home", str(tmp_path))

    monkeypatch.setattr(
        exporter.subprocess, "check_output", lambda cmd: DU_OUTPUT.encode()
    )
    exporter.update_metrics("work", str(tmp_path))

    assert gauge_value("af_work_dir_used_kb") == 52428800
    assert gauge_value("af_work_dir_size_kb") == exporter.WORK_QUOTA_KB
    # home gauges keep their own values
    assert gauge_value("af_home_dir_used_kb") == 5242880


# ── one unreadable directory must not stop the others ─────────────────────────


def fail(_cmd):
    raise exporter.subprocess.CalledProcessError(1, "du")


def test_update_directory_reports_success(monkeypatch):
    monkeypatch.setattr(
        exporter.subprocess, "check_output", lambda cmd: DF_OUTPUT.encode()
    )

    assert exporter.update_directory("home", "/home/alice") is True
    assert gauge_value("af_home_dir_ok") == 1


def test_update_directory_swallows_an_unreadable_mount(monkeypatch):
    """`du: cannot access '/work/users/<user>/': Permission denied` used to
    raise out of the loop and kill the sidecar — 275 restarts on one pod."""
    monkeypatch.setattr(exporter.subprocess, "check_output", fail)

    assert exporter.update_directory("work", "/work/users/alice/") is False
    assert gauge_value("af_work_dir_ok") == 0


def test_a_failing_work_dir_leaves_home_metrics_alone(monkeypatch):
    """/home utilisation is what the quota alerts fire on, so it has to survive
    /work being unreadable."""
    monkeypatch.setattr(
        exporter.subprocess, "check_output", lambda cmd: DF_OUTPUT.encode()
    )
    exporter.update_directory("home", "/home/alice")

    monkeypatch.setattr(exporter.subprocess, "check_output", fail)
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
