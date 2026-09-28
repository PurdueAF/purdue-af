"""Tests for apps/ray-train — the shared Ray cluster that Ray Train jobs run on.

The properties the deployment depends on and no schema checks: GPU pods exist
only while jobs need them, the head takes no work, every pod sees the same
storage and can write only the results directory the head creates, nothing
outside the cluster reaches more than the Jobs API, and a spec change reaches
the running pods.
"""

import pytest
import yaml
from common import REPO

APP = REPO / "apps" / "ray-train"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"
JOBS_API = [{"protocol": "TCP", "port": 8265}]


def load(path):
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="module")
def values():
    return load(APP / "values.yaml")


@pytest.fixture(scope="module")
def release():
    return load(APP / "helmrelease.yaml")


def test_flux_deploys_the_cluster():
    kustomization = load(EXPERIMENTAL)
    for name in ("helmrelease", "networkpolicy"):
        assert f"../../apps/ray-train/{name}.yaml" in kustomization["resources"]
    generators = {g["name"]: g for g in kustomization["configMapGenerator"]}
    assert generators["ray-train-config"]["files"] == [
        "values.yaml=../../apps/ray-train/values.yaml"
    ]


def test_release_waits_for_the_crds(release):
    """The chart's RayCluster needs the operator's ray.io CRDs first, from an
    operator release the same root deploys."""
    (dependency,) = release["spec"]["dependsOn"]
    operator = REPO / "apps" / "ray" / "operator" / "helmrelease.yaml"
    assert load(operator)["metadata"]["name"] == dependency["name"]
    assert "../../apps/ray/operator/helmrelease.yaml" in load(EXPERIMENTAL)["resources"]


def test_spec_changes_recreate_the_pods(release, values):
    """KubeRay applies a changed spec only to pods created afterwards unless
    upgradeStrategy is Recreate: new workers would join a head on the old
    spec. The chart has no value for it, so a post-renderer adds it."""
    (renderer,) = release["spec"]["postRenderers"]
    (patch,) = renderer["kustomize"]["patches"]
    assert patch["target"] == {
        "kind": "RayCluster",
        "name": values["fullnameOverride"],
    }
    assert yaml.safe_load(patch["patch"]) == [
        {"op": "add", "path": "/spec/upgradeStrategy", "value": {"type": "Recreate"}}
    ]


def test_gpu_pods_exist_only_while_jobs_need_them(values):
    worker = values["worker"]
    assert values["head"]["enableInTreeAutoscaling"] is True
    assert worker["replicas"] == 0
    assert worker["minReplicas"] == 0
    assert worker["maxReplicas"] >= 1


def test_the_autoscaler_sees_the_workers_gpu(values):
    """The autoscaler counts only limits named *gpu; any other GPU resource,
    a MIG device for one, must be declared as num-gpus or no pod is ever
    added for a GPU request."""
    worker = values["worker"]
    gpus = {
        name: count
        for name, count in worker["resources"]["limits"].items()
        if name.startswith("nvidia.com/")
    }
    assert sum(gpus.values()) == 1
    for name, count in gpus.items():
        if not name.endswith("gpu"):
            assert int(worker["rayStartParams"]["num-gpus"]) == count


def test_the_head_takes_no_work(values):
    head = values["head"]
    assert head["rayStartParams"]["num-cpus"] == "0"
    assert not any(
        name.startswith("nvidia.com/") for name in head["resources"]["limits"]
    )


def test_every_pod_mounts_the_same_storage(values):
    """Checkpoints from every worker meet at one path, and a job's driver on
    the head reads its data where the workers do."""
    assert values["head"]["volumes"] == values["worker"]["volumes"]
    assert values["head"]["volumeMounts"] == values["worker"]["volumeMounts"]


def test_only_the_results_directory_is_writable(values):
    volumes = {v["name"]: v for v in values["worker"]["volumes"]}
    writable = [
        m
        for m in values["worker"]["volumeMounts"]
        if not m.get("readOnly") and "emptyDir" not in volumes[m["name"]]
    ]
    (results,) = writable
    (work,) = [
        m
        for m in values["worker"]["volumeMounts"]
        if m["name"] == results["name"] and "subPath" not in m
    ]
    assert work["readOnly"] is True
    assert results["mountPath"] == f"{work['mountPath']}/{results['subPath']}"


def test_the_head_creates_the_results_directory(values):
    """The results directory is a subPath of the shared claim: it must exist,
    owned by the Ray user, before a pod mounts it."""
    (results,) = [m for m in values["worker"]["volumeMounts"] if "subPath" in m]
    (init,) = values["head"]["initContainers"]
    (mount,) = init["volumeMounts"]
    assert mount["name"] == results["name"]
    assert init["command"][0] == "install"
    assert init["command"][-1] == f"{mount['mountPath']}/{results['subPath']}"


def test_only_the_jobs_api_is_reachable_from_outside_the_cluster(values):
    """The Jobs API runs whatever it is sent, unauthenticated."""
    policy = load(APP / "networkpolicy.yaml")
    own_pods = {
        "podSelector": {"matchLabels": {"ray.io/cluster": values["fullnameOverride"]}}
    }
    assert policy["spec"]["podSelector"] == own_pods["podSelector"]
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    for rule in policy["spec"]["ingress"]:
        if rule["from"] != [own_pods]:
            assert rule.get("ports") == JOBS_API
    assert values["service"]["type"] == "ClusterIP"
