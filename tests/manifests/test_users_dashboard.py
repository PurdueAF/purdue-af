"""The "Last active" column on the Users Statistics dashboard.

It used to read `time() - af_home_dir_last_accessed` — the st_atime of the
user's home directory, which measures the mount rather than the session. On
this NFS that column was years stale for most rows, and identical for every
row at once whenever a sweep touched /home, so genuinely idle sessions looked
freshly active. It has to come from the hub's own last_activity, the signal
the cullers act on."""

import json

import yaml
from common import REPO

DASHBOARD = REPO / "apps/monitoring/grafana/dashboards/users-overview.json"
SNIPPET = REPO / "apps/jupyterhub/jupyterhub/extraFiles/session-activity.py"
OVERLAYS = ("core-production", "core-geddes2")
METRIC = "af_session_last_activity_seconds"


def panel():
    doc = json.loads(DASHBOARD.read_text())
    return next(p for p in doc["panels"] if p.get("type") == "table")


def column_named(display_name):
    """(target, override) feeding the column shown under `display_name`."""
    override = next(
        o
        for o in panel()["fieldConfig"]["overrides"]
        for prop in o["properties"]
        if prop["id"] == "displayName" and prop["value"] == display_name
    )
    ref = override["matcher"]["options"].removeprefix("Value #")
    target = next(t for t in panel()["targets"] if t["refId"] == ref)
    return target, override


def test_last_active_reads_the_hub_not_the_filesystem():
    target, _ = column_named("Last active")
    assert target["expr"] == f'time() - {METRIC}{{job="jupyterhub"}}'
    assert "af_home_dir_last_accessed" not in json.dumps(panel())


def test_last_active_joins_the_table_on_pod():
    """Every query in this panel is merged by `pod`, so the metric has to carry
    that label — which is why the exporter renders the spawner's pod name."""
    joins = [t for t in panel()["transformations"] if t["id"] == "joinByField"]
    assert [t["options"]["byField"] for t in joins] == ["pod"]
    assert '"pod"' in SNIPPET.read_text()


def test_thresholds_track_the_cull_timeouts():
    """A row turns yellow once the GPU culler would take it (24h) and red once
    the global culler would (14d); anything older should no longer exist."""
    _, override = column_named("Last active")
    steps = next(
        p["value"]["steps"] for p in override["properties"] if p["id"] == "thresholds"
    )
    assert [s["value"] for s in steps] == [None, 86400, 604800, 1209600]


def test_the_exporter_is_deployed_to_every_cluster():
    for overlay in OVERLAYS:
        path = REPO / "deploy" / overlay / "kustomization.yaml"
        generators = yaml.safe_load(path.read_text())["configMapGenerator"]
        config = next(g for g in generators if g["name"] == "jupyterhub-extra-config")
        assert any(f.endswith(f"extraFiles/{SNIPPET.name}") for f in config["files"]), (
            overlay
        )
