"""Contracts for the Global pixi env panels on the default dashboard.

These panels have burned operators before: a sticky last-duration gauge that
looks like continuous work, a state-timeline that rendered "-∞+", and a status
stat that said OK while the daemon had not rebuilt for days. Hold the queries
that fix those."""

import json

from common import REPO

DASHBOARD = REPO / "apps/monitoring/grafana/dashboards/default.json"


def panels_by_title():
    doc = json.loads(DASHBOARD.read_text())
    return {p["title"]: p for p in doc["panels"]}


def test_pixi_panels_exist():
    titles = panels_by_title()
    assert "Global pixi env — sync status" in titles
    assert "Global pixi env — sync/health history" in titles
    assert "Global pixi env — sync activity" in titles


def test_status_distinguishes_daemon_liveness_from_in_sync():
    """in_sync alone is not proof the loop is alive or recently rebuilt."""
    panel = panels_by_title()["Global pixi env — sync status"]
    legends = {t["legendFormat"] for t in panel["targets"]}
    assert {"in sync", "env healthy", "sync mode", "daemon", "last rebuild"} <= legends
    daemon = next(t for t in panel["targets"] if t["legendFormat"] == "daemon")
    assert "loop_heartbeat_timestamp_seconds" in daemon["expr"]
    rebuild = next(t for t in panel["targets"] if t["legendFormat"] == "last rebuild")
    assert "last_success_timestamp_seconds" in rebuild["expr"]


def test_history_timeline_hides_raw_numeric_range():
    """showValue=auto is what printed '-∞+' across every row."""
    panel = panels_by_title()["Global pixi env — sync/health history"]
    assert panel["type"] == "state-timeline"
    assert panel["options"]["showValue"] == "never"
    for target in panel["targets"]:
        assert "== bool" in target["expr"] or "< bool" in target["expr"], target["expr"]


def test_activity_duration_only_plots_when_a_sync_finished():
    """last_sync_duration_seconds is sticky — gate it on syncs_total increase."""
    panel = panels_by_title()["Global pixi env — sync activity"]
    duration = next(t for t in panel["targets"] if "duration" in t["legendFormat"])
    assert "last_sync_duration_seconds" in duration["expr"]
    assert "increase(pixi_global_sync_syncs_total" in duration["expr"]
    assert "> 0" in duration["expr"]
