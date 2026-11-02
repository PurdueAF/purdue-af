"""Read the Grafana dashboards the way their tests do."""

import json

from common import REPO

DASHBOARDS = REPO / "apps/monitoring/grafana/dashboards"


def dashboard(name):
    return json.loads((DASHBOARDS / name).read_text())


def panel(name, title):
    """The one panel titled `title`: a row may share a section's title, nothing else may."""
    (match,) = [
        p
        for p in dashboard(name)["panels"]
        if p.get("title") == title and p["type"] != "row"
    ]
    return match
