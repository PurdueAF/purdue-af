"""Mount heatmap queries must join on node_pool.

Otherwise a timeout series left under a node's inactive pool is summed in.
"""

import json

from common import REPO

DEFAULT = REPO / "apps/monitoring/grafana/dashboards/default.json"

MOUNT_PANEL_TITLES = ("Depot mount", "/work/ mount", "EOS mount", "CVMFS mount")


def _exprs(path, titles):
    doc = json.loads(path.read_text())
    for panel in doc["panels"]:
        if panel.get("title") not in titles:
            continue
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if "af_node_mount_" in expr:
                yield panel.get("title", ""), expr


def test_default_mount_heatmaps_join_on_node_pool():
    found = list(_exprs(DEFAULT, MOUNT_PANEL_TITLES))
    assert len(found) == 4
    for title, expr in found:
        assert "on(node, mount_name, node_pool)" in expr, (title, expr)
        assert "on(node, mount_name)" not in expr.replace(
            "on(node, mount_name, node_pool)", ""
        ), (title, expr)
