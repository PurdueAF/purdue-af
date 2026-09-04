"""Tests for apps/ray — the Ray cluster mirroring the SuperSONIC release.

The point of this suite is the *mirroring*: the value of sonic-ray is that it
runs on the same hardware, at the same size, with the same replica range as
`supersonic`, so the two can be compared. If somebody retunes supersonic and
forgets Ray, these tests say so.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
RAY = REPO / "apps" / "ray"
SUPERSONIC = REPO / "apps" / "sonic" / "supersonic" / "values.yaml"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"


def load(path):
    return yaml.safe_load(path.read_text())


def ray_values():
    return load(RAY / "sonic-ray" / "values.yaml")


def supersonic_values():
    return load(SUPERSONIC)


def test_flux_deploys_operator_and_cluster():
    text = EXPERIMENTAL.read_text()
    for resource in (
        "../../apps/ray/helmrepo.yaml",
        "../../apps/ray/operator/helmrelease.yaml",
        "../../apps/ray/sonic-ray/helmrelease.yaml",
        "../../apps/ray/sonic-ray/metrics-service.yaml",
    ):
        assert resource in text
        for line in text.splitlines():
            if resource in line:
                assert not line.strip().startswith("#"), line


def test_values_reach_the_releases():
    """Every valuesFrom ConfigMap must actually be generated from a values.yaml
    — a name mismatch leaves the release running on chart defaults."""
    kustomization = load(EXPERIMENTAL)
    generated = {cm["name"]: cm["files"] for cm in kustomization["configMapGenerator"]}

    for app, config in (
        ("operator", "kuberay-operator-config"),
        ("sonic-ray", "sonic-ray-config"),
    ):
        hr = load(RAY / app / "helmrelease.yaml")
        assert [v["name"] for v in hr["spec"]["valuesFrom"]] == [config]
        assert generated[config] == [f"values.yaml=../../apps/ray/{app}/values.yaml"]


def test_cluster_waits_for_the_crds():
    """The ray-cluster chart renders a RayCluster; without the operator's CRDs
    the release fails and retries forever."""
    hr = load(RAY / "sonic-ray" / "helmrelease.yaml")
    assert hr["spec"]["dependsOn"] == [{"name": "kuberay-operator"}]

    operator = load(RAY / "operator" / "helmrelease.yaml")
    assert operator["spec"]["install"]["crds"] == "Create"
    assert operator["spec"]["upgrade"]["crds"] == "CreateReplace"


def test_operator_stays_namespaced():
    """singleNamespaceInstall keeps RBAC (and the watch) inside cms."""
    assert load(RAY / "operator" / "values.yaml")["singleNamespaceInstall"] is True


def test_gpu_workers_match_triton():
    """Same node pool, same taint, same per-server resources as Triton."""
    worker = ray_values()["worker"]
    triton = supersonic_values()["triton"]

    assert worker["resources"] == triton["resources"]
    assert worker["nodeSelector"] == triton["nodeSelector"]
    assert worker["tolerations"] == triton["tolerations"]


def test_head_lands_on_the_same_nodes():
    head = ray_values()["head"]
    triton = supersonic_values()["triton"]

    assert head["nodeSelector"] == triton["nodeSelector"]
    assert head["tolerations"] == triton["tolerations"]
    # The head routes; it never holds a model. Ray tasks must stay off it.
    assert head["rayStartParams"]["num-cpus"] == "0"


def test_replica_range_matches_keda():
    worker = ray_values()["worker"]
    keda = supersonic_values()["keda"]

    assert worker["minReplicas"] == keda["minReplicaCount"]
    assert worker["maxReplicas"] == keda["maxReplicaCount"]
    # Ray's own autoscaler stands in for the KEDA ScaledObject.
    assert ray_values()["head"]["enableInTreeAutoscaling"] is True


def test_entry_point_is_the_private_pool():
    """Same as envoy: a LoadBalancer on geddes-private-pool, never an ingress."""
    values = ray_values()
    assert values["service"]["type"] == "LoadBalancer"
    annotations = values["head"]["headService"]["metadata"]["annotations"]
    assert annotations["metallb.universe.tf/address-pool"] == "geddes-private-pool"


def test_serve_port_is_exposed_on_the_head():
    ports = {p["name"]: p["containerPort"] for p in ray_values()["head"]["ports"]}
    # KubeRay's own defaults, which explicit ports replace, plus serve.
    assert ports == {
        "gcs-server": 6379,
        "client": 10001,
        "dashboard": 8265,
        "metrics": 8080,
        "serve": 8000,
    }


def test_model_repository_is_mounted_read_only():
    """Ray reads the repository the model manager owns; it must never write."""
    worker = ray_values()["worker"]
    volume = next(v for v in worker["volumes"] if v["name"] == "model-repository")
    claim = volume["persistentVolumeClaim"]
    assert (
        claim["claimName"]
        == supersonic_values()["triton"]["modelRepository"]["pvc"]["claimName"]
    )
    assert claim["readOnly"] is True

    mount = next(m for m in worker["volumeMounts"] if m["name"] == "model-repository")
    assert mount["readOnly"] is True

    # Ray's own logs still need somewhere to go.
    assert any(m["mountPath"] == "/tmp/ray" for m in worker["volumeMounts"])


def test_metrics_service_is_scraped_like_triton():
    svc = load(RAY / "sonic-ray" / "metrics-service.yaml")
    labels = svc["metadata"]["labels"]
    # The label the AF Prometheus `pods` job keeps on.
    assert labels["scrape_metrics"] == "true"
    assert labels["app.kubernetes.io/instance"] == "sonic-ray"
    # Head and workers both, hence the cluster label rather than a node type.
    assert svc["spec"]["selector"] == {"ray.io/cluster": "sonic-ray"}
    assert [p["port"] for p in svc["spec"]["ports"]] == [8080]
