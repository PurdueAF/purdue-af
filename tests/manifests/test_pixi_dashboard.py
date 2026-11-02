"""Contracts for the Global pixi env panels on the default dashboard.

A duration panel must not hold its last value, the state timeline must render
finite values, and the status stat must go stale when rebuilds stop."""

from dashboard_helpers import panel

DASHBOARD = "default.json"


def test_status_distinguishes_daemon_liveness_from_in_sync():
    """in_sync alone is not proof the loop is alive or recently rebuilt."""
    status = panel(DASHBOARD, "Global pixi env — sync status")
    legends = {t["legendFormat"] for t in status["targets"]}
    assert {"in sync", "env healthy", "sync mode", "daemon", "last rebuild"} <= legends
    daemon = next(t for t in status["targets"] if t["legendFormat"] == "daemon")
    assert "loop_heartbeat_timestamp_seconds" in daemon["expr"]
    rebuild = next(t for t in status["targets"] if t["legendFormat"] == "last rebuild")
    assert "last_success_timestamp_seconds" in rebuild["expr"]


def test_history_timeline_hides_raw_numeric_range():
    """showValue=auto is what printed '-∞+' across every row."""
    history = panel(DASHBOARD, "Global pixi env — sync/health history")
    assert history["type"] == "state-timeline"
    assert history["options"]["showValue"] == "never"
    for target in history["targets"]:
        assert "== bool" in target["expr"] or "< bool" in target["expr"], target["expr"]


def test_activity_duration_only_plots_when_a_sync_finished():
    """last_sync_duration_seconds is sticky — gate it on syncs_total increase."""
    activity = panel(DASHBOARD, "Global pixi env — sync activity")
    duration = next(t for t in activity["targets"] if "duration" in t["legendFormat"])
    assert "last_sync_duration_seconds" in duration["expr"]
    assert "increase(pixi_global_sync_syncs_total" in duration["expr"]
    assert "> 0" in duration["expr"]
