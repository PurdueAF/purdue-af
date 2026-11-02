"""The Dask Gateway and Ray on Geddes stats split running workers by priority class.

Each names the class its gateway gives the workers past the guaranteed ones; a
class renamed in the gateway alone would count every worker as guaranteed.
"""

import re

import pytest
from common import REPO
from dashboard_helpers import panel

DASK_RELEASE = REPO / "apps/dask-gateway/helmrelease.yaml"
GPU_QUERIES = REPO / "apps/jupyterhub/jupyterhub/extraFiles/gpu_queries.py"


def _classes():
    (dask,) = set(re.findall(r'PREEMPTIBLE = "([^"]+)"', DASK_RELEASE.read_text()))
    (ray,) = set(
        re.findall(r'PREEMPTIBLE_PRIORITY_CLASS = "([^"]+)"', GPU_QUERIES.read_text())
    )
    return {"Dask Gateway": dask, "Ray on Geddes": ray}


def _exprs(title):
    targets = panel("default.json", title)["targets"]
    return {t["legendFormat"]: t["expr"] for t in targets}


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
def test_the_stats_are_typeset_alike_with_ray_under_dask(dashboard):
    """Automatic text sizes follow each panel's own digits, so the sizes are fixed."""
    dask, *rays = (
        panel(dashboard, title)
        for title in ("Dask Gateway", "Ray on Geddes", "Ray on NRP")
    )
    assert dask["options"]["text"].keys() == {"titleSize", "valueSize"}
    assert all(ray["options"] == dask["options"] for ray in rays)
    above = dask["gridPos"]
    for ray in rays:
        below = ray["gridPos"]
        assert below["y"] >= above["y"] + above["h"]
        assert (
            above["x"] <= below["x"]
            and below["x"] + below["w"] <= above["x"] + above["w"]
        )
