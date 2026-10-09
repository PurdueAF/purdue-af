"""The Dask Gateway and Ray stats split running workers by priority class.

Each names the class its gateway gives the workers past the guaranteed ones; a
class renamed in the gateway alone would count every worker as guaranteed.
"""

import json
import re

import pytest
import yaml
from common import REPO

DASHBOARDS = REPO / "apps/monitoring/grafana/dashboards"
DASK_RELEASE = REPO / "apps/dask-gateway/helmrelease.yaml"
RAY_CONFIG = REPO / "apps/ray-train/config.yaml"


def _classes():
    (dask,) = set(re.findall(r'PREEMPTIBLE = "([^"]+)"', DASK_RELEASE.read_text()))
    ray = yaml.safe_load(RAY_CONFIG.read_text())["preemptiblePriorityClass"]
    return {"Dask Gateway": dask, "Ray": ray}


def _stat(title, dashboard="default.json"):
    doc = json.loads((DASHBOARDS / dashboard).read_text())
    (panel,) = [p for p in doc["panels"] if p["type"] == "stat" and p["title"] == title]
    return panel


def _exprs(title):
    return {t["legendFormat"]: t["expr"] for t in _stat(title)["targets"]}


def test_split_names_the_class_each_gateway_assigns():
    for title, priority_class in _classes().items():
        exprs = _exprs(title)
        guaranteed, preemptible = (
            exprs["guaranteed workers"],
            exprs["preemptible workers"],
        )
        assert f'priority_class!="{priority_class}"' in guaranteed, title
        assert f'priority_class="{priority_class}"' in preemptible, title


def test_both_halves_count_the_same_running_pods():
    for title in _classes():
        exprs = _exprs(title)
        guaranteed = exprs["guaranteed workers"].replace(
            "priority_class!=", "priority_class="
        )
        assert guaranteed == exprs["preemptible workers"], title
        assert 'phase="Running"' in guaranteed, title


@pytest.mark.parametrize("dashboard", ["default.json", "phys390.json"])
def test_the_two_stats_are_stacked_and_typeset_alike(dashboard):
    """Automatic text sizes follow each panel's own digits, so the sizes are fixed."""
    dask, ray = (_stat(title, dashboard) for title in ("Dask Gateway", "Ray"))
    assert dask["options"] == ray["options"]
    assert dask["options"]["text"].keys() == {"titleSize", "valueSize"}
    legends = [[t["legendFormat"] for t in p["targets"]] for p in (dask, ray)]
    assert legends[0] == legends[1]
    above, below = dask["gridPos"], ray["gridPos"]
    assert (above["x"], above["w"], above["h"]) == (below["x"], below["w"], below["h"])
    assert below["y"] == above["y"] + above["h"]
