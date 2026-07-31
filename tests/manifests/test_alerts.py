"""Tests for the Prometheus alert rules and the health dashboard they feed.

Alerts are the platform's definition of "unhealthy" — the Grafana health table
and (next) the MCP health tool both read `ALERTS`, so a rule that loses its
`component` label silently drops out of both. These tests hold that contract.
Expression correctness is checked by promtool in validate-manifests.sh; what
cannot be checked there is whether the labels line up with the consumers."""

import json
import re

import yaml
from common import REPO

VALUES = REPO / "apps/monitoring/prometheus/values.yaml"
DASHBOARD = REPO / "apps/monitoring/grafana/dashboards/alerts.json"
SEVERITIES = {"warning", "error", "critical"}


def rules():
    doc = yaml.safe_load(VALUES.read_text())
    groups = doc["serverFiles"]["alerting_rules.yml"]["groups"]
    return [r for g in groups for r in g["rules"]]


def test_every_alert_has_a_component_and_severity():
    """The health table groups by component and splits by severity; an alert
    missing either is invisible in the only view that summarises health."""
    for rule in rules():
        labels = rule.get("labels", {})
        assert labels.get("component"), f"{rule['alert']} has no component label"
        assert labels.get("severity") in SEVERITIES, (
            f"{rule['alert']}: severity {labels.get('severity')!r} not in {SEVERITIES}"
        )


def test_every_alert_explains_what_to_do():
    """A firing alert an operator cannot act on is noise in the health signal."""
    for rule in rules():
        annotations = rule.get("annotations", {})
        assert annotations.get("summary"), f"{rule['alert']} has no summary"
        assert len(annotations.get("description", "")) > 40, (
            f"{rule['alert']} has no usable description"
        )


def test_every_alert_waits_before_firing():
    """Without `for:`, a single bad scrape flips the facility to unhealthy."""
    for rule in rules():
        assert rule.get("for"), f"{rule['alert']} fires instantly"


def test_alert_names_are_prefixed():
    for rule in rules():
        assert rule["alert"].startswith("AF"), rule["alert"]


def test_hub_alert_is_scoped_to_this_environment():
    """The jupyterhub job is a static target list; an unscoped `up == 0`
    would fire for any other hub added to it."""
    hub = next(r for r in rules() if r["alert"] == "AFHubDown")
    assert "${jupyterhub_host}" in hub["expr"], (
        "AFHubDown must be scoped to the environment's own hub"
    )


def test_mount_invalid_is_a_warning_not_an_error():
    """Transient storage failures must not flip Facility health / MCP Degraded."""
    invalid = next(r for r in rules() if r["alert"] == "AFMountInvalid")
    assert invalid["labels"]["severity"] == "warning"
    slow = next(r for r in rules() if r["alert"] == "AFMountSlow")
    assert slow["labels"]["severity"] == "warning"


def test_mount_invalid_requires_fresh_completed_check():
    """Red (AFMountInvalid) is only a finished failing check — never a missing
    series from a NotReady node (those publish null and do not match)."""
    invalid = next(r for r in rules() if r["alert"] == "AFMountInvalid")
    assert "af_node_mount_valid" in invalid["expr"]
    assert "af_node_mount_result_fresh" in invalid["expr"]
    assert "== 1" in invalid["expr"]


def test_prod_nodes_not_ready_uses_cms_af_taint():
    """NotReady AF capacity must page operators; mount gauges are null there."""
    rule = next(r for r in rules() if r["alert"] == "AFProdNodesNotReady")
    assert 'value="cms-af"' in rule["expr"], rule["expr"]
    assert "kube_node_status_condition" in rule["expr"]
    assert rule["labels"]["component"] == "compute"
    assert rule["labels"]["severity"] == "error"


def test_mount_health_unknown_waits_ten_minutes():
    """Unknown used to wait 30m while EOS timeouts sat pending; keep it short."""
    unknown = next(r for r in rules() if r["alert"] == "AFMountHealthUnknown")
    assert unknown["for"] == "10m"


def test_af_pod_monitor_scrape_does_not_steal_node_label():
    """af-node-monitor exports node=<AF worker>. Scraping the monitor pod's
    kubernetes node name into the same label renames the worker to exported_node
    and makes non-AF hosts (e.g. where the Deployment sits) look monitored."""
    doc = yaml.safe_load(VALUES.read_text())
    jobs = doc["serverFiles"]["prometheus.yml"]["scrape_configs"]
    af = next(j for j in jobs if j["job_name"] == "af-pod-monitor")
    node_targets = [
        r
        for r in af["relabel_configs"]
        if r.get("source_labels") == ["__meta_kubernetes_pod_node_name"]
    ]
    assert node_targets, "expected a pod-node relabel"
    assert all(r.get("target_label") != "node" for r in node_targets), node_targets
    assert any(r.get("target_label") == "pod_node" for r in node_targets)


def test_mount_alerts_name_the_worker_node_label():
    """Summaries must use the exporter's node label, not a scrape-host alias."""
    for name in (
        "AFMountInvalid",
        "AFMountSlow",
        "AFMountHealthUnknown",
        "AFMountInvalidDev",
    ):
        rule = next(r for r in rules() if r["alert"] == name)
        summary = rule["annotations"]["summary"]
        assert "$labels.node" in summary, summary
        assert "exported_node" not in summary


def test_mount_slow_excludes_the_probe_timeout_sentinel():
    """af-node-monitor reports its timeout value (10000 ms) as the latency when
    a check gives up; AFMountInvalid covers that case. Without the upper bound
    both alerts fire for one fault."""
    slow = next(r for r in rules() if r["alert"] == "AFMountSlow")
    assert "< 10000" in slow["expr"], slow["expr"]


def test_mount_slow_uses_a_rolling_average_on_prod():
    """Mount checks refresh only every ~10 minutes and individual samples often
    dip below the threshold while EOS is still chronically slow. Instant
    comparisons with `for:` never hold; avg_over_time does. Prod-only so a
    sick cms-af-dev node cannot affect the user-facing signal."""
    slow = next(r for r in rules() if r["alert"] == "AFMountSlow")
    assert "avg_over_time" in slow["expr"], slow["expr"]
    assert 'node_pool="prod"' in slow["expr"], slow["expr"]


# --- the dashboard that turns alerts into a health answer -----------------


def dashboard():
    return json.loads(DASHBOARD.read_text())


def panels_by_title():
    return {p["title"]: p for p in dashboard()["panels"]}


def test_health_panels_exist():
    titles = panels_by_title()
    assert "Facility health" in titles
    assert "Firing alerts by component" in titles


def test_health_stat_counts_only_actionable_severities():
    """Warnings are routine; a headline that goes red on every warning stops
    being read."""
    expr = panels_by_title()["Facility health"]["targets"][0]["expr"]
    assert 'alertstate="firing"' in expr
    assert "error|critical" in expr
    # `or vector(0)` keeps the panel green instead of "No data" when nothing
    # is firing — the common and most important case
    assert "or vector(0)" in expr


def test_component_table_groups_by_the_alert_labels():
    panel = panels_by_title()["Firing alerts by component"]
    expr = panel["targets"][0]["expr"]
    assert re.search(r"count by \(component, severity\)", expr), expr
    matrix = next(t for t in panel["transformations"] if t["id"] == "groupingToMatrix")
    assert matrix["options"]["rowField"] == "component"
    assert matrix["options"]["columnField"] == "severity"


def test_dashboard_covers_every_component_that_has_alerts():
    """Nothing to configure per component — but if the table ever moves to a
    hardcoded list, this catches the omission."""
    components = {r["labels"]["component"] for r in rules()}
    assert len(components) >= 3, components
    panel = json.dumps(panels_by_title()["Firing alerts by component"])
    hardcoded = [c for c in components if f'"{c}"' in panel]
    assert not hardcoded, f"table hardcodes components: {hardcoded}"
