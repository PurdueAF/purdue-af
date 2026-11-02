"""The "Last active" column on the Users Statistics dashboard.

It comes from the hub's last_activity, the signal the cullers act on."""

import yaml
from common import REPO
from dashboard_helpers import dashboard

SNIPPET = REPO / "apps/jupyterhub/jupyterhub/extraFiles/session-activity.py"
HUB_VALUES = REPO / "apps/jupyterhub/jupyterhub/values.yaml"
OVERLAYS = ("core-production", "core-geddes2")
METRIC = "af_session_last_activity_seconds"


def table():
    panels = dashboard("users-overview.json")["panels"]
    return next(p for p in panels if p.get("type") == "table")


def column_named(display_name):
    """(target, override) feeding the column shown under `display_name`."""
    override = next(
        o
        for o in table()["fieldConfig"]["overrides"]
        for prop in o["properties"]
        if prop["id"] == "displayName" and prop["value"] == display_name
    )
    ref = override["matcher"]["options"].removeprefix("Value #")
    target = next(t for t in table()["targets"] if t["refId"] == ref)
    return target, override


def test_last_active_reads_the_hub():
    target, _ = column_named("Last active")
    assert target["expr"] == f'time() - {METRIC}{{job="jupyterhub"}}'


def test_last_active_joins_the_table_on_pod():
    """Every query in this panel is merged by `pod`, so the metric has to carry
    that label — which is why the exporter renders the spawner's pod name."""
    joins = [t for t in table()["transformations"] if t["id"] == "joinByField"]
    assert [t["options"]["byField"] for t in joins] == ["pod"]
    assert '"pod"' in SNIPPET.read_text()


def test_thresholds_track_the_cull_timeouts():
    """A row turns yellow once the GPU culler would take it and red once the
    global culler would."""
    values = yaml.safe_load(HUB_VALUES.read_text())
    (gpu_culler,) = [
        argument.removeprefix("--timeout=")
        for argument in values["hub"]["services"]["gpu-culler"]["command"]
        if argument.startswith("--timeout=")
    ]
    _, override = column_named("Last active")
    steps = next(
        p["value"]["steps"] for p in override["properties"] if p["id"] == "thresholds"
    )
    assert [s["value"] for s in steps] == [
        None,
        int(gpu_culler),
        604800,
        values["cull"]["timeout"],
    ]


def test_the_exporter_is_deployed_to_every_cluster():
    for overlay in OVERLAYS:
        path = REPO / "flux" / overlay / "kustomization.yaml"
        generators = yaml.safe_load(path.read_text())["configMapGenerator"]
        config = next(g for g in generators if g["name"] == "jupyterhub-extra-config")
        assert any(f.endswith(f"extraFiles/{SNIPPET.name}") for f in config["files"]), (
            overlay
        )
