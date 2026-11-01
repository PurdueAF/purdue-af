"""Tests for .github/workflows/normalize-dashboards.py.

The strip must never change what a dashboard draws: a default goes only when it
equals Grafana's, and a value-less override property goes only where it clears
nothing."""

import json

from common import REPO, load_script

SCRIPT = REPO / ".github" / "workflows" / "normalize-dashboards.py"
DASHBOARDS = REPO / "apps/monitoring/grafana/dashboards"
PROMETHEUS = {"type": "prometheus", "uid": "prometheus"}
RANCHER = {"type": "prometheus", "uid": "prometheus-rancher"}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}


def norm():
    return load_script(SCRIPT, "normalize_dashboards")


def strip(panel):
    dashboard = {"id": 7, "version": 3, "uid": "x", "panels": [panel]}
    norm().normalize(dashboard)
    assert "id" not in dashboard and "version" not in dashboard
    return dashboard["panels"][0]


def timeseries(custom):
    return {"type": "timeseries", "fieldConfig": {"defaults": {"custom": custom}}}


def test_timeseries_defaults_go_and_choices_stay():
    panel = strip(
        timeseries({"lineWidth": 1, "fillOpacity": 10, "showPoints": "never"})
    )
    assert panel["fieldConfig"]["defaults"]["custom"] == {
        "fillOpacity": 10,
        "showPoints": "never",
    }


def test_a_custom_of_only_defaults_goes():
    panel = strip(timeseries({"lineWidth": 1}))
    assert "custom" not in panel["fieldConfig"]["defaults"]


def test_a_default_stays_on_another_panel_type():
    panel = strip(
        {"type": "barchart", "fieldConfig": {"defaults": {"custom": {"lineWidth": 1}}}}
    )
    assert panel["fieldConfig"]["defaults"]["custom"] == {"lineWidth": 1}


def test_stacking_goes_only_when_it_is_off():
    panel = strip(timeseries({"stacking": {"group": "A", "mode": "normal"}}))
    assert panel["fieldConfig"]["defaults"]["custom"]["stacking"]["mode"] == "normal"


def test_a_target_datasource_goes_only_when_it_is_the_panels():
    panel = strip(
        {
            "datasource": PROMETHEUS,
            "targets": [{"datasource": PROMETHEUS}, {"datasource": RANCHER}],
        }
    )
    assert panel["targets"] == [{}, {"datasource": RANCHER}]


def test_mixed_with_one_datasource_becomes_that_datasource():
    panel = strip(
        {
            "datasource": MIXED,
            "targets": [{"datasource": RANCHER}, {"datasource": RANCHER}],
        }
    )
    assert panel["datasource"] == RANCHER
    assert panel["targets"] == [{}, {}]


def test_mixed_with_two_datasources_stays():
    targets = [{"datasource": PROMETHEUS}, {"datasource": RANCHER}]
    panel = strip({"datasource": MIXED, "targets": [dict(t) for t in targets]})
    assert panel["datasource"] == MIXED
    assert panel["targets"] == targets


def test_a_set_interval_stays():
    panel = strip({"targets": [{"interval": "1h"}, {"interval": ""}]})
    assert panel["targets"] == [{"interval": "1h"}, {}]


def override(*properties):
    return {"matcher": {"id": "byName", "options": "a"}, "properties": list(properties)}


def test_a_bare_property_goes_where_it_clears_nothing():
    panel = strip(
        {"fieldConfig": {"defaults": {}, "overrides": [override({"id": "decimals"})]}}
    )
    assert "overrides" not in panel["fieldConfig"]


def test_a_bare_property_stays_where_the_defaults_set_its_path():
    bare = override({"id": "custom.width"})
    panel = strip(
        {"fieldConfig": {"defaults": {"custom": {"width": 90}}, "overrides": [bare]}}
    )
    assert panel["fieldConfig"]["overrides"] == [bare]


def test_a_bare_property_stays_where_an_earlier_override_sets_its_path():
    first = override({"id": "decimals", "value": 2})
    second = override({"id": "decimals"})
    panel = strip({"fieldConfig": {"defaults": {}, "overrides": [first, second]}})
    assert panel["fieldConfig"]["overrides"] == [first, second]


def test_nested_panels_are_normalized():
    row = strip({"type": "row", "panels": [{"pluginVersion": "13.2.4"}]})
    assert row["panels"] == [{}]


def test_the_committed_dashboards_are_normalized():
    for path in DASHBOARDS.glob("*.json"):
        before = json.loads(path.read_text())
        after = json.loads(path.read_text())
        norm().normalize(after)
        assert after == before, path.name
