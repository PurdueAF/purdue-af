"""Tests for the Prometheus alert rules and the health dashboard they feed.

The Grafana health table and the MCP health tool both read `ALERTS`, so every
rule carries the labels they group by. What each expression does is tested in
alerts.test.yml, which validate-manifests.sh runs with promtool."""

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


def jobs():
    doc = yaml.safe_load(VALUES.read_text())
    return doc["serverFiles"]["prometheus.yml"]["scrape_configs"]


def af_pod_monitor_drops():
    af = next(j for j in jobs() if j["job_name"] == "af-pod-monitor")
    return [
        r
        for r in af["relabel_configs"]
        if r.get("action") == "drop"
        and r.get("source_labels") == ["__meta_kubernetes_service_name"]
    ]


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
        assert annotations.get("description"), f"{rule['alert']} has no description"


def test_every_alert_waits_before_firing():
    """Without `for:`, a single bad scrape flips the facility to unhealthy."""
    for rule in rules():
        assert rule.get("for"), f"{rule['alert']} fires instantly"


def test_alert_names_are_prefixed():
    for rule in rules():
        assert rule["alert"].startswith("AF"), rule["alert"]


def test_hub_alert_is_scoped_to_this_environment():
    """The job's one target is production's host on every cluster, so an
    unscoped `up == 0` on geddes2 would report a hub that is not its own."""
    hub = next(r for r in rules() if r["alert"] == "AFHubDown")
    assert "${jupyterhub_host}" in hub["expr"], (
        "AFHubDown must be scoped to the environment's own hub"
    )


def test_login_callback_alert_is_scoped_to_this_environment():
    """Both sides of the `unless` read the same hub, or one environment's
    successes would clear another's failures."""
    rule = next(r for r in rules() if r["alert"] == "AFLoginCallbackFailing")
    assert rule["expr"].count('instance="${jupyterhub_host}"') == 2, rule["expr"]


def test_af_pod_monitor_does_not_double_scrape_pixi_sync():
    """pixi-global-sync has its own static scrape; kubernetes SD must not
    duplicate those series under job=af-pod-monitor."""
    drops = af_pod_monitor_drops()
    assert any("pixi-global-sync" in str(r.get("regex", "")) for r in drops), drops
    assert any(j["job_name"] == "pixi-global-sync" for j in jobs())


def test_kube_state_metrics_scrape_keeps_only_cms_and_cluster_scoped():
    """Cluster-wide KSM (~244k series) is what balloons TSDB on this PVC and
    stretches Ceph compaction long enough for liveness to SIGKILL the pod."""
    ksm = next(j for j in jobs() if j["job_name"] == "kube-state-metrics")
    keeps = [
        r
        for r in ksm.get("metric_relabel_configs", [])
        if r.get("action") == "keep" and r.get("source_labels") == ["namespace"]
    ]
    assert keeps, "expected a namespace keep rule on kube-state-metrics"
    assert keeps[0].get("regex") == "(|cms)"


def test_prometheus_server_tolerates_ceph_stalls():
    """Default chart probes kill this server on WAL/compaction stalls."""
    doc = yaml.safe_load(VALUES.read_text())
    server = doc["server"]
    assert server["startupProbe"]["enabled"] is True
    assert (
        server["startupProbe"]["failureThreshold"]
        * server["startupProbe"]["periodSeconds"]
        >= 300
    )
    assert (
        server["livenessProbeFailureThreshold"] * server["livenessProbePeriodSeconds"]
        >= 120
    )
    assert "query.timeout=2m" in server["extraFlags"]
    assert server["global"]["scrape_timeout"] < server["global"]["scrape_interval"]


def test_prometheus_tsdb_uses_rwo_block_storage():
    """CephFS/RWX freezes this server during WAL/compaction (probes time out
    for hours at ~0 CPU), so the TSDB sits on RBD RWO, like Grafana's."""
    pvc = yaml.safe_load((REPO / "apps/monitoring/prometheus/pvc.yaml").read_text())
    assert pvc["spec"]["storageClassName"] == "${singlenode_storage_class}"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    doc = yaml.safe_load(VALUES.read_text())
    assert doc["server"]["persistentVolume"]["existingClaim"] == "prometheus-data-pvc"


def test_af_node_monitor_has_dedicated_scrape_without_pod_host():
    """Mount metrics must not carry the Deployment host (geddes-b001 etc.)."""
    node_mon = next(j for j in jobs() if j["job_name"] == "af-node-monitor")
    targets = node_mon["static_configs"][0]["targets"]
    assert "af-node-monitor-service:8000" in targets
    drops = af_pod_monitor_drops()
    assert any("af-node-monitor-service" in str(r.get("regex", "")) for r in drops), (
        drops
    )


def test_af_pod_monitor_scrape_does_not_steal_node_label():
    """af-node-monitor exports node=<AF worker>. Scraping the monitor pod's
    kubernetes node name into the same label renames the worker to exported_node
    and makes non-AF hosts (e.g. where the Deployment sits) look monitored."""
    af = next(j for j in jobs() if j["job_name"] == "af-pod-monitor")
    node_targets = [
        r
        for r in af["relabel_configs"]
        if r.get("source_labels") == ["__meta_kubernetes_pod_node_name"]
    ]
    assert node_targets, "expected a pod-node relabel"
    assert all(r.get("target_label") != "node" for r in node_targets), node_targets
    assert any(r.get("target_label") == "pod_node" for r in node_targets)


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


def test_a_stuck_session_alerts_before_the_hub_deletes_the_pod():
    """A rule that waits past the spawn timeout can never see an ordinary stuck spawn."""
    rule = next(r for r in rules() if r["alert"] == "AFSessionStuckPending")
    minutes = int(re.fullmatch(r"(\d+)m", rule["for"]).group(1))
    config = (
        REPO / "apps/jupyterhub/jupyterhub/extraFiles/set-user-info.py"
    ).read_text()
    timeout = int(re.search(r"KubeSpawner\.start_timeout = (\d+)", config).group(1))
    assert minutes * 60 < timeout


def test_the_alert_lists_name_every_rule():
    """Most AF rules aggregate the namespace label away, so a filter on it hides
    them. The datasource keeps Rancher's cluster-wide alerts off the AF lists."""
    panels = panels_by_title()
    for title in ("Errors", "Warnings"):
        options = panels[title]["options"]
        assert options["datasource"] == "prometheus", title
        assert "namespace" not in options["alertInstanceLabelFilter"], title
    rancher = panels["Rancher alerts in cms"]["options"]
    assert rancher["datasource"] == "prometheus-rancher"
    assert rancher["alertInstanceLabelFilter"] == 'namespace="cms"'
