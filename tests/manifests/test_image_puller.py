"""The Hub's continuous image puller holds the images of the pods users start
on demand, on the nodes those pods land on."""

import pytest
import yaml
from common import REPO

HUB_VALUES = REPO / "apps" / "jupyterhub" / "jupyterhub" / "values.yaml"
RAY_CLUSTER = REPO / "apps" / "ray-train" / "raycluster.yaml"
DASK_VALUES = REPO / "apps" / "dask-gateway" / "values.yaml"


def load(path):
    return yaml.safe_load(path.read_text())


def ray_clusters():
    pod = load(RAY_CLUSTER)["spec"]["headGroupSpec"]["template"]["spec"]
    (container,) = pod["containers"]
    return container["image"], [pod["nodeSelector"]]


def dask_clusters():
    backend = load(DASK_VALUES)["gateway"]["backend"]
    image = f"{backend['image']['name']}:{backend['image']['tag']}"
    roles = ("scheduler", "worker")
    return image, [backend[role]["extraPodConfig"]["nodeSelector"] for role in roles]


@pytest.mark.parametrize(
    "entry, pods", [("ray-train", ray_clusters), ("dask-gateway", dask_clusters)]
)
def test_the_puller_holds_the_image_where_its_pods_run(entry, pods):
    """The puller runs where sessions do, and these pods land on some of those nodes."""
    values = load(HUB_VALUES)
    puller = values["prePuller"]
    assert puller["continuous"]["enabled"]
    pulled = puller["extraImages"][entry]
    image, node_selectors = pods()
    assert f"{pulled['name']}:{pulled['tag']}" == image
    sessions = values["singleuser"]["nodeSelector"]
    for node_selector in node_selectors:
        assert sessions.items() <= node_selector.items()
