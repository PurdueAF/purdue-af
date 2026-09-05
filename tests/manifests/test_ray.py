"""Tests for apps/ray — the supersonic release's model repository, served by
Ray Serve.

Two jobs.

One: the release serves *supersonic's* models. It mounts the claim the model
manager writes and supersonic's Triton reads, at the same path, on the same
nodes, behind the same address pool — and this file keeps that equal to
apps/sonic/supersonic/values.yaml so retuning one and forgetting the other
turns the build red.

Two: the properties the scaling loop depends on, which no schema can
express: a replica is one GPU and a pod is one replica, Serve can never ask
for more replicas than the worker group may hold, nothing runs on the head,
the Services select on labels KubeRay leaves alone, and the chart's Ray
version is the image's. Those run against the rendered chart when helm is on
PATH (it is in CI), and against the values and source otherwise.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
RAY = REPO / "apps" / "ray"
CHART = RAY / "sonic-ray" / "chart"
VALUES = RAY / "sonic-ray" / "values.yaml"
IMAGE = REPO / "docker" / "sonic-ray"
SERVE_APP = IMAGE / "sonic_ray" / "serve_app.py"
SUPERSONIC = REPO / "apps" / "sonic" / "supersonic" / "values.yaml"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"
VALIDATOR = REPO / ".github" / "workflows" / "validate-manifests.sh"
IMAGE_INPUTS = REPO / ".github" / "workflows" / "image-inputs.sh"
CI_IMAGES = REPO / ".github" / "workflows" / "ci-images.yml"


def load(path):
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="module")
def values():
    return load(VALUES)


@pytest.fixture(scope="module")
def chart_defaults():
    return load(CHART / "values.yaml")


@pytest.fixture(scope="module")
def supersonic():
    return load(SUPERSONIC)


@pytest.fixture(scope="module")
def rendered():
    """Every object the chart renders with the AF values, keyed by kind/name."""
    if shutil.which("helm") is None:
        pytest.skip("helm not on PATH; validate-manifests.sh renders this chart in CI")
    out = subprocess.run(
        [
            "helm",
            "template",
            "sonic-ray",
            str(CHART),
            "--namespace",
            "cms",
            "-f",
            str(VALUES),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(out)
        if doc
    }


@pytest.fixture(scope="module")
def rayservice(rendered):
    return rendered[("RayService", "sonic-ray")]


@pytest.fixture(scope="module")
def cluster(rayservice):
    return rayservice["spec"]["rayClusterConfig"]


@pytest.fixture(scope="module")
def serve_config(rayservice):
    return yaml.safe_load(rayservice["spec"]["serveConfigV2"])


@pytest.fixture(scope="module")
def deployment(serve_config):
    apps = serve_config["applications"]
    assert len(apps) == 1 and len(apps[0]["deployments"]) == 1
    return apps[0]["deployments"][0]


@pytest.fixture(scope="module")
def head_pod(cluster):
    return cluster["headGroupSpec"]["template"]


@pytest.fixture(scope="module")
def worker_group(cluster):
    groups = cluster["workerGroupSpecs"]
    assert len(groups) == 1, "one replica is one GPU pod; more groups needs a rethink"
    return groups[0]


def container(pod_template, name):
    return next(c for c in pod_template["spec"]["containers"] if c["name"] == name)


def env_of(container_spec):
    return {e["name"]: e["value"] for e in container_spec["env"]}


# -- Flux wiring -----------------------------------------------------------


def test_flux_deploys_operator_and_release():
    text = EXPERIMENTAL.read_text()
    for resource in (
        "../../apps/ray/helmrepo.yaml",
        "../../apps/ray/operator/helmrelease.yaml",
        "../../apps/ray/sonic-ray/helmrelease.yaml",
    ):
        assert resource in text
        for line in text.splitlines():
            if resource in line:
                assert not line.strip().startswith("#"), line


def test_values_reach_both_releases():
    """A valuesFrom ConfigMap nobody generates leaves a release on chart defaults."""
    generated = {
        cm["name"]: cm["files"] for cm in load(EXPERIMENTAL)["configMapGenerator"]
    }
    for app, config in (
        ("operator", "kuberay-operator-config"),
        ("sonic-ray", "sonic-ray-config"),
    ):
        hr = load(RAY / app / "helmrelease.yaml")
        assert [v["name"] for v in hr["spec"]["valuesFrom"]] == [config]
        assert generated[config] == [f"values.yaml=../../apps/ray/{app}/values.yaml"]


def test_release_waits_for_the_crds():
    """The chart renders a RayService. Until the operator's chart has installed
    the ray.io CRDs that is an unknown kind, and a raw manifest in the same
    Kustomization would have blocked the apply that installs them."""
    release = load(RAY / "sonic-ray" / "helmrelease.yaml")
    assert release["spec"]["dependsOn"] == [{"name": "kuberay-operator"}]
    assert release["spec"]["chart"]["spec"]["chart"] == "./apps/ray/sonic-ray/chart"
    assert release["spec"]["chart"]["spec"]["sourceRef"]["kind"] == "GitRepository"

    operator = load(RAY / "operator" / "helmrelease.yaml")
    assert operator["spec"]["install"]["crds"] == "Create"
    assert operator["spec"]["upgrade"]["crds"] == "CreateReplace"
    # singleNamespaceInstall keeps the watch and the RBAC inside cms.
    assert load(RAY / "operator" / "values.yaml")["singleNamespaceInstall"] is True


def test_validator_renders_this_chart():
    """Nothing else validates a chart sourced from this repository: kubeconform
    never sees what helm renders, and ray.io has no schema anyway."""
    text = VALIDATOR.read_text()
    assert "from this repository" in text
    assert "RayService" not in text, "the kubeconform skip is gone; keep it gone"


# -- the image ---------------------------------------------------------------


def test_image_is_built_and_published_by_ci():
    """An aux image nobody builds is a pod that never pulls."""
    assert "sonic-ray)" in IMAGE_INPUTS.read_text()
    ci = CI_IMAGES.read_text()
    assert "- name: sonic-ray" in ci
    assert re.search(r"for name in .*\bsonic-ray\b.*; do", ci), (
        "publish loop misses sonic-ray"
    )
    assert (IMAGE / "Dockerfile").is_file()


def test_chart_pulls_the_ci_image(chart_defaults):
    image = chart_defaults["image"]
    assert image["repository"].endswith("/ghcr-proxy-cache/purdueaf/sonic-ray")
    assert image["tag"] == "latest"
    assert image["pullPolicy"] == "Always", (
        ":latest moves; a cached pull would pin an old build"
    )


def test_ray_version_is_the_images(chart_defaults):
    """The autoscaler sidecar KubeRay adds is pinned to rayVersion; a mismatch
    with the Ray inside the image is a cluster that never comes up."""
    dockerfile = (IMAGE / "Dockerfile").read_text()
    match = re.search(
        r"^FROM rayproject/ray:(\d+\.\d+\.\d+)-", dockerfile, re.MULTILINE
    )
    assert match, "Dockerfile FROM must be an official rayproject/ray tag"
    assert chart_defaults["ray"]["version"] == match.group(1)


def test_onnxruntime_is_the_cuda12_line():
    """The Ray base image carries CUDA 12.8; onnxruntime-gpu 1.27+ links
    against CUDA 13. Renovate holds the pin — make sure it still does."""
    pyproject = (IMAGE / "pyproject.toml").read_text()
    match = re.search(r'"onnxruntime-gpu==(\d+)\.(\d+)\.\d+"', pyproject)
    assert match and (int(match.group(1)), int(match.group(2))) < (1, 27)
    renovate = (REPO / ".github" / "renovate.json5").read_text()
    assert "onnxruntime-gpu" in renovate and "'<1.27'" in renovate


# -- the models are supersonic's ---------------------------------------------


def test_model_repository_is_supersonics(values, supersonic):
    theirs = supersonic["triton"]["modelRepository"]
    assert values["modelRepository"]["claimName"] == theirs["pvc"]["claimName"]
    assert values["modelRepository"]["mountPath"] == theirs["mountPath"]


def test_pods_land_where_supersonic_pods_land(values, supersonic):
    assert values["nodeSelector"] == supersonic["triton"]["nodeSelector"]
    assert values["tolerations"] == supersonic["triton"]["tolerations"]
    pool = values["service"]["annotations"]["metallb.universe.tf/address-pool"]
    assert (
        pool
        == supersonic["triton"]["service"]["annotations"][
            "metallb.universe.tf/address-pool"
        ]
    )


def test_model_repository_is_mounted_read_only_on_workers(
    worker_group, head_pod, values
):
    """Replicas only read it; the model manager owns the writes. The head runs
    no replica and mounts nothing."""
    volume = next(
        v
        for v in worker_group["template"]["spec"]["volumes"]
        if v["name"] == "model-repository"
    )
    assert volume["persistentVolumeClaim"] == {
        "claimName": values["modelRepository"]["claimName"],
        "readOnly": True,
    }
    worker = container(worker_group["template"], "ray-worker")
    mount = next(m for m in worker["volumeMounts"] if m["name"] == "model-repository")
    assert mount == {
        "name": "model-repository",
        "mountPath": "/models",
        "readOnly": True,
    }
    assert env_of(worker)["MODEL_REPOSITORY"] == mount["mountPath"]
    assert all(v["name"] != "model-repository" for v in head_pod["spec"]["volumes"])


def test_placement_applies_to_every_pod(head_pod, worker_group, values):
    for template in (head_pod, worker_group["template"]):
        assert template["spec"]["nodeSelector"] == values["nodeSelector"]
        assert template["spec"]["tolerations"] == values["tolerations"]


# -- scaling: a replica is a GPU, a pod is a replica -------------------------


def test_no_keda_resources():
    """A ScaledObject would be a second controller fighting Ray over the
    group — and could not drive it anyway: RayCluster has no scale subresource."""
    for path in sorted(RAY.rglob("*.yaml")):
        text = path.read_text()
        if "templates" in path.parts:  # Go templates, not YAML until rendered
            assert "ScaledObject" not in text and "keda.sh" not in text, path
            continue
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict):
                assert doc.get("kind") != "ScaledObject", path
                assert "keda.sh" not in str(doc.get("apiVersion", "")), path


def test_replica_is_one_gpu_and_pod_is_one_replica(deployment, worker_group, cluster):
    assert deployment["ray_actor_options"] == {"num_gpus": 1}
    worker = container(worker_group["template"], "ray-worker")
    assert worker["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert worker["resources"]["requests"]["nvidia.com/gpu"] == 1
    assert cluster["enableInTreeAutoscaling"] is True


def test_serve_cannot_outgrow_the_worker_group(deployment, worker_group):
    """A replica with no GPU pod to land on pends forever (the chart refuses
    to render otherwise)."""
    autoscaling = deployment["autoscaling_config"]
    assert autoscaling["max_replicas"] <= worker_group["maxReplicas"]
    assert autoscaling["min_replicas"] <= autoscaling["max_replicas"]
    assert worker_group["minReplicas"] <= worker_group["maxReplicas"]
    assert worker_group["replicas"] == worker_group["minReplicas"]


def test_scale_down_is_slower_than_scale_up(deployment):
    autoscaling = deployment["autoscaling_config"]
    assert autoscaling["downscale_delay_s"] > autoscaling["upscale_delay_s"]
    assert autoscaling["target_ongoing_requests"] < deployment["max_ongoing_requests"]


def test_replicas_are_given_time_to_drain(deployment, worker_group):
    """Scale-down deletes the pod; graceful_shutdown_timeout_s is worth nothing
    if the kubelet does not wait for it. (The chart refuses to render otherwise.)"""
    assert (
        worker_group["template"]["spec"]["terminationGracePeriodSeconds"]
        > deployment["graceful_shutdown_timeout_s"]
    )


def test_nothing_runs_on_the_head(cluster, head_pod):
    """The head holds the Serve controller and a proxy; a replica there would
    need a GPU the head does not have."""
    assert cluster["headGroupSpec"]["rayStartParams"]["num-cpus"] == "0"
    head = container(head_pod, "ray-head")
    assert "nvidia.com/gpu" not in head["resources"]["limits"]


def test_serve_import_path_resolves(serve_config, deployment):
    """import_path names a module in the image and an attribute in it; the
    deployment name is the class serve.deployment wraps."""
    module_path, _, attribute = serve_config["applications"][0][
        "import_path"
    ].partition(":")
    assert module_path == "sonic_ray.serve_app"
    source = SERVE_APP.read_text()
    assert re.search(
        rf"^{attribute} = {deployment['name']}\.bind\(\)", source, re.MULTILINE
    )
    assert re.search(rf"^class {deployment['name']}\b", source, re.MULTILINE)


def test_environment_reaches_head_and_workers_alike(
    head_pod, worker_group, chart_defaults
):
    """An import on either must see the same configuration."""
    head_env = env_of(container(head_pod, "ray-head"))
    worker_env = env_of(container(worker_group["template"], "ray-worker"))
    assert head_env == worker_env
    assert head_env["ONNX_EXECUTION_PROVIDERS"] == chart_defaults["executionProviders"]
    assert head_env["ONNX_EXECUTION_PROVIDERS"].startswith("CUDAExecutionProvider")


# -- services ---------------------------------------------------------------


def test_inference_entry_point_is_kuberays_serve_service(rayservice):
    """Envoy's job in the supersonic release: one address on the private
    pool. KubeRay keeps it pointed at pods whose Serve proxy is healthy."""
    svc = rayservice["spec"]["serveService"]
    assert svc["metadata"]["name"] == "sonic-ray-serve"
    assert (
        svc["metadata"]["annotations"]["metallb.universe.tf/address-pool"]
        == "geddes-private-pool"
    )
    assert svc["spec"]["type"] == "LoadBalancer"
    assert [(p["port"], p["targetPort"]) for p in svc["spec"]["ports"]] == [
        (8000, 8000)
    ]
    assert "scrape_metrics" not in svc["metadata"]["labels"]


def test_head_is_not_exposed(cluster):
    """The dashboard has no auth. Inference has its own address."""
    assert cluster["headGroupSpec"]["serviceType"] == "ClusterIP"
    assert "headService" not in cluster["headGroupSpec"]


def test_metrics_service_selects_labels_kuberay_leaves_alone(
    rendered, head_pod, worker_group
):
    """KubeRay stamps app.kubernetes.io/name onto every pod it creates and
    names the cluster <service>-raycluster-<hash>, renamed on each upgrade.
    Selecting on either would match nothing, silently."""
    svc = rendered[("Service", "sonic-ray-metrics")]
    assert svc["metadata"]["labels"]["scrape_metrics"] == "true"
    assert svc["spec"]["clusterIP"] == "None"
    assert [p["port"] for p in svc["spec"]["ports"]] == [8080]
    assert svc["spec"]["selector"].items() <= head_pod["metadata"]["labels"].items()
    assert (
        svc["spec"]["selector"].items()
        <= worker_group["template"]["metadata"]["labels"].items()
    )
    assert "app.kubernetes.io/name" not in svc["spec"]["selector"]
    assert "ray.io/cluster" not in svc["spec"]["selector"]
    # release="sonic-ray" is how the AF dashboards will select ray_serve_*.
    assert svc["metadata"]["labels"]["app.kubernetes.io/instance"] == "sonic-ray"
    for template in (head_pod, worker_group["template"]):
        ports = {
            p["name"]
            for c in template["spec"]["containers"]
            for p in c.get("ports", [])
        }
        assert "metrics" in ports


# -- the chart refuses what cannot work ----------------------------------------


@pytest.mark.parametrize(
    "override, message",
    [
        ("modelRepository.claimName=", "claimName is required"),
        ("serve.maxReplicas=9", "exceeds ray.worker.maxReplicas"),
        ("serve.minReplicas=5", "serve.minReplicas exceeds"),
        (
            "ray.worker.resources.limits.nvidia\\.com/gpu=2",
            "exactly one nvidia.com/gpu",
        ),
        (
            "ray.worker.terminationGracePeriodSeconds=30",
            "must exceed serve.gracefulShutdownTimeoutS",
        ),
    ],
)
def test_chart_fails_on_values_that_cannot_work(override, message):
    if shutil.which("helm") is None:
        pytest.skip("helm not on PATH")
    result = subprocess.run(
        [
            "helm",
            "template",
            "sonic-ray",
            str(CHART),
            "-f",
            str(VALUES),
            "--set",
            override,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert message in result.stderr
