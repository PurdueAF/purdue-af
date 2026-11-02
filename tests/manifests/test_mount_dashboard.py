"""Mount heatmap queries must join on node_pool.

Otherwise a timeout series left under a node's inactive pool is summed in.
"""

from dashboard_helpers import panel

MOUNT_PANEL_TITLES = ("Depot mount", "/work/ mount", "EOS mount", "CVMFS mount")


def test_default_mount_heatmaps_join_on_node_pool():
    found = [
        (title, target["expr"])
        for title in MOUNT_PANEL_TITLES
        for target in panel("default.json", title)["targets"]
        if "af_node_mount_" in target.get("expr", "")
    ]
    assert len(found) == 4
    for title, expr in found:
        assert "on(node, mount_name, node_pool)" in expr, (title, expr)
        assert "on(node, mount_name)" not in expr.replace(
            "on(node, mount_name, node_pool)", ""
        ), (title, expr)
