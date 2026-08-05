"""Mount heatmap queries must join on node_pool.

Without node_pool in the join, a ghost timeout series under the inactive pool
(fresh=0, latency=10000) is pulled into the sum whenever any pool for that
node is fresh — which is what painted paf-b00 red until cms-af-prod was added.
"""

import json

from common import REPO

DEFAULT = REPO / "apps/monitoring/grafana/dashboards/default.json"
PHYS390 = REPO / "apps/monitoring/grafana/dashboards/phys390.json"

MOUNT_PANEL_TITLES = ("Depot mount", "/work/ mount", "EOS mount", "CVMFS mount")


def _exprs(path, titles=None):
    doc = json.loads(path.read_text())
    for panel in doc["panels"]:
        if titles is not None and panel.get("title") not in titles:
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


def test_phys390_mount_panels_join_on_node_pool():
    found = [e for _, e in _exprs(PHYS390) if "af_node_mount_result_fresh" in e]
    assert found
    for expr in found:
        assert "on(node, mount_name, node_pool)" in expr, expr
