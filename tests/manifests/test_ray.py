"""Tests for apps/ray — Triton on Ray with Ray Serve carrying its gRPC.

The properties the deployment depends on, which no schema can express: the
Triton container is the one the values describe, on a read-only model
repository; one forwarding replica per Triton pod (the `triton` resource), Serve
never asking for more replicas than the worker group may hold, nothing on the
head, Serve's gRPC proxy handed Triton's own servicer from a package every
pod installs, the Services selecting on labels KubeRay leaves alone. Those
run against the rendered chart when helm is on PATH (it is in CI), and
against the values and source otherwise.
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
CODE = CHART / "files" / "sonic_ray"
SERVE_APP = CODE / "serve_app.py"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"
VALIDATOR = REPO / ".github" / "workflows" / "validate-manifests.sh"


def load(path):
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="module")
def values():
    return load(VALUES)


@pytest.fixture(scope="module")
def chart_defaults():
    return load(CHART / "values.yaml")


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
    assert len(groups) == 1, (
        "one replica is one Triton pod; more groups needs a rethink"
    )
    return groups[0]


def container(pod_template, name, kind="containers"):
    return next(c for c in pod_template["spec"][kind] if c["name"] == name)


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


# -- no custom image -----------------------------------------------------------


def test_pods_run_stock_images(head_pod, worker_group, chart_defaults, values):
    """Official Ray (CPU flavour: no Ray process touches a GPU) through the
    Docker Hub proxy cache, official Triton straight from NVIDIA. The Ray
    tag's version is ray.version,
    which also pins the autoscaler sidecar KubeRay adds."""
    ray = chart_defaults["ray"]
    ray_image = (
        f"{ray['image']['repository']}:{ray['version']}-{ray['image']['flavor']}"
    )
    assert ray_image.startswith(
        "geddes-registry.rcac.purdue.edu/docker-hub-cache/rayproject/ray:"
    )
    assert ray_image.endswith("-cpu")
    for template, name in (
        (head_pod, "ray-head"),
        (worker_group["template"], "ray-worker"),
    ):
        c = container(template, name)
        assert c["image"] == ray_image
        assert c["imagePullPolicy"] == "IfNotPresent"  # immutable tags
        assert (
            container(template, "pip-install", "initContainers")["image"] == ray_image
        )
    triton = container(worker_group["template"], "triton")
    assert (
        triton["image"]
        == f"{values['triton']['image']['repository']}:{values['triton']['image']['tag']}"
    )
    assert not (REPO / "docker" / "sonic-ray").exists(), "no custom image, by decision"


def test_tritons_servicer_is_installed_where_the_proxies_run(
    head_pod, worker_group, chart_defaults
):
    """Serve's gRPC proxy imports Triton's generated servicer at startup, on
    every node, outside any runtime_env — so the package must be on PYTHONPATH
    for the whole pod. --no-deps keeps the image's grpcio/protobuf in charge;
    tritonclient 2.48 is the last release whose stubs match protobuf 4."""
    pins = chart_defaults["python"]["pip"]
    (tc,) = [p for p in pins if p.startswith("tritonclient==")]
    major, minor = map(int, tc.removeprefix("tritonclient==").split(".")[:2])
    assert (major, minor) <= (2, 48)
    for template, name in (
        (head_pod, "ray-head"),
        (worker_group["template"], "ray-worker"),
    ):
        init = container(template, "pip-install", "initContainers")
        assert init["args"] == pins
        assert "--no-deps" in init["command"] and "--target" in init["command"]
        deps_dir = init["command"][init["command"].index("--target") + 1]
        deps_mount = next(m for m in init["volumeMounts"] if m["name"] == "python-deps")
        assert deps_mount["mountPath"] == deps_dir
        c = container(template, name)
        assert deps_dir in env_of(c)["PYTHONPATH"].split(":")
        assert any(
            m["name"] == "python-deps" and m["mountPath"] == deps_dir
            for m in c["volumeMounts"]
        )
        assert any(
            v["name"] == "python-deps" and "emptyDir" in v
            for v in template["spec"]["volumes"]
        )


def test_code_reaches_every_pod(rendered, head_pod, worker_group):
    """import_path resolves only if the ConfigMap holds the package and lands
    on PYTHONPATH — on the head (where Serve builds the application) and the
    workers (where replicas run). A code change must roll the cluster."""
    configmap = rendered[("ConfigMap", "sonic-ray-code")]
    files = {p.name: p.read_text() for p in CODE.glob("*.py")}
    assert files and configmap["data"] == files
    for template, name in (
        (head_pod, "ray-head"),
        (worker_group["template"], "ray-worker"),
    ):
        volume = next(v for v in template["spec"]["volumes"] if v["name"] == "code")
        assert volume["configMap"]["name"] == "sonic-ray-code"
        c = container(template, name)
        mount = next(m for m in c["volumeMounts"] if m["name"] == "code")
        assert mount["readOnly"] is True
        code_dir = mount["mountPath"].removesuffix("/sonic_ray")
        assert code_dir in env_of(c)["PYTHONPATH"].split(":")
        assert template["metadata"]["annotations"]["checksum/code"]
    assert (
        head_pod["metadata"]["annotations"]["checksum/code"]
        == worker_group["template"]["metadata"]["annotations"]["checksum/code"]
    )


# -- the Triton in the pod is the one in the values ---------------------------


def test_rendered_triton_is_the_one_in_values(worker_group, values):
    """The values are only parity if the template actually uses them."""
    triton = container(worker_group["template"], "triton")
    assert triton["command"] == values["triton"]["command"]
    assert triton["args"] == values["triton"]["args"]
    assert triton["resources"] == values["triton"]["resources"]
    assert triton["readinessProbe"]["successThreshold"] == 3
    assert {p["name"]: p["containerPort"] for p in triton["ports"]} == {
        "http": 8000,
        "grpc": 8001,
        "triton-metrics": 8002,
    }


def test_model_repository_is_mounted_read_only(worker_group, values):
    """Triton only reads it; the model manager owns the writes."""
    volume = next(
        v
        for v in worker_group["template"]["spec"]["volumes"]
        if v["name"] == "model-repository"
    )
    assert volume["persistentVolumeClaim"] == {
        "claimName": values["triton"]["modelRepository"]["claimName"],
        "readOnly": True,
    }
    mount = next(
        m
        for m in container(worker_group["template"], "triton")["volumeMounts"]
        if m["name"] == "model-repository"
    )
    assert mount == {
        "name": "model-repository",
        "mountPath": "/models",
        "readOnly": True,
    }


def test_placement_applies_to_every_pod(head_pod, worker_group, values):
    for template in (head_pod, worker_group["template"]):
        assert template["spec"]["nodeSelector"] == values["nodeSelector"]
        assert template["spec"]["tolerations"] == values["tolerations"]


# -- scaling: one replica per Triton pod, all of it Ray's -------------------


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


def test_one_replica_per_triton_pod(deployment, worker_group, cluster):
    """Each worker advertises one `triton`; each replica claims one. Nothing
    else does, so a pod without a replica is idle and reclaimable, and a
    replica without a pod is the pending request that grows the group."""
    assert deployment["ray_actor_options"] == {
        "num_cpus": 0,
        "resources": {"triton": 1},
    }
    assert '\\"triton\\": 1' in worker_group["rayStartParams"]["resources"]
    assert "resources" not in cluster["headGroupSpec"]["rayStartParams"]
    assert cluster["enableInTreeAutoscaling"] is True
    # The GPU is Triton's; Ray never sees it and never schedules onto it.
    ray_worker = container(worker_group["template"], "ray-worker")
    assert "nvidia.com/gpu" not in ray_worker["resources"]["limits"]
    assert (
        container(worker_group["template"], "triton")["resources"]["limits"][
            "nvidia.com/gpu"
        ]
        == 1
    )


def test_serve_cannot_outgrow_the_worker_group(deployment, worker_group):
    """A replica with no Triton pod to land on pends forever (the chart refuses
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


def test_triton_is_given_time_to_drain(worker_group, deployment):
    """Scale-down deletes the pod; --exit-timeout-secs and Serve's graceful
    shutdown are worth nothing if the kubelet does not wait for them. (The
    chart refuses to render otherwise.)"""
    args = container(worker_group["template"], "triton")["args"][0]
    exit_timeout = int(args.split("--exit-timeout-secs=")[1].split()[0])
    grace = worker_group["template"]["spec"]["terminationGracePeriodSeconds"]
    assert grace > exit_timeout
    assert grace > deployment["graceful_shutdown_timeout_s"]


def test_nothing_runs_on_the_head(cluster, head_pod):
    """The head holds the Serve controller and proxies; a replica there would
    have no Triton to forward to."""
    assert cluster["headGroupSpec"]["rayStartParams"]["num-cpus"] == "0"
    assert all(c["name"] != "triton" for c in head_pod["spec"]["containers"])


# -- Serve speaks Triton's protocol -------------------------------------------


def test_grpc_proxy_is_handed_tritons_servicer(serve_config):
    """The proxy accepts exactly the RPCs of Triton's GRPCInferenceService and
    dispatches each to the replica method of the same name — which the
    forwarder defines for every one of them (tests/sonic_ray)."""
    grpc = serve_config["grpc_options"]
    assert grpc["grpc_servicer_functions"] == [
        "tritonclient.grpc.service_pb2_grpc.add_GRPCInferenceServiceServicer_to_server"
    ]
    assert grpc["port"] == 9000


def test_serve_import_path_resolves(serve_config, deployment):
    """import_path names a module in the ConfigMap and an attribute in it; the
    deployment name is the class serve.deployment wraps."""
    module_path, _, attribute = serve_config["applications"][0][
        "import_path"
    ].partition(":")
    assert module_path == "sonic_ray.serve_app"
    source = SERVE_APP.read_text()
    assert re.search(
        rf"^{attribute} = serve\.deployment\({deployment['name']}\)\.bind\(\)",
        source,
        re.MULTILINE,
    )
    assert re.search(rf"^class {deployment['name']}\b", source, re.MULTILINE)


# -- services ---------------------------------------------------------------


def test_inference_entry_point_is_kuberays_serve_service(
    rayservice, head_pod, worker_group
):
    """One gRPC address on the private pool, on Triton's conventional port. Behind it is Serve's gRPC proxy, so
    every request is counted. KubeRay keeps the Service pointed at pods whose
    proxy is healthy."""
    svc = rayservice["spec"]["serveService"]
    assert svc["metadata"]["name"] == "sonic-ray-serve"
    assert (
        svc["metadata"]["annotations"]["metallb.universe.tf/address-pool"]
        == "geddes-private-pool"
    )
    assert svc["spec"]["type"] == "LoadBalancer"
    ports = {p["name"]: (p["port"], p["targetPort"]) for p in svc["spec"]["ports"]}
    assert ports["grpc"] == (8001, 9000)
    assert "scrape_metrics" not in svc["metadata"]["labels"]
    for template, name in (
        (head_pod, "ray-head"),
        (worker_group["template"], "ray-worker"),
    ):
        assert {p["containerPort"] for p in container(template, name)["ports"]} >= {
            8000,
            9000,
        }


def test_head_is_not_exposed(cluster):
    """The dashboard has no auth. Inference has its own address."""
    assert cluster["headGroupSpec"]["serviceType"] == "ClusterIP"
    assert "headService" not in cluster["headGroupSpec"]


def test_metrics_services_select_labels_kuberay_leaves_alone(
    rendered, head_pod, worker_group
):
    """KubeRay stamps app.kubernetes.io/name onto every pod it creates and
    names the cluster <service>-raycluster-<hash>, renamed on each upgrade.
    Selecting on either would match nothing, silently."""
    head_labels = head_pod["metadata"]["labels"]
    worker_labels = worker_group["template"]["metadata"]["labels"]

    ray_metrics = rendered[("Service", "sonic-ray-metrics")]
    assert ray_metrics["metadata"]["labels"]["scrape_metrics"] == "true"
    assert [p["port"] for p in ray_metrics["spec"]["ports"]] == [8080]
    assert ray_metrics["spec"]["selector"].items() <= head_labels.items()
    assert ray_metrics["spec"]["selector"].items() <= worker_labels.items()

    triton_metrics = rendered[("Service", "sonic-ray-triton-metrics")]
    assert triton_metrics["metadata"]["labels"]["scrape_metrics"] == "true"
    assert [p["port"] for p in triton_metrics["spec"]["ports"]] == [8002]
    assert triton_metrics["spec"]["selector"].items() <= worker_labels.items()
    # Only the workers run Triton; scraping 8002 on the head would just fail.
    assert not triton_metrics["spec"]["selector"].items() <= head_labels.items()

    for svc in (ray_metrics, triton_metrics):
        assert "app.kubernetes.io/name" not in svc["spec"]["selector"]
        assert "ray.io/cluster" not in svc["spec"]["selector"]
        # release="sonic-ray" is how dashboards select this release's series.
        assert svc["metadata"]["labels"]["app.kubernetes.io/instance"] == "sonic-ray"


# -- the chart refuses what cannot work ----------------------------------------


@pytest.mark.parametrize(
    "override, message",
    [
        ("triton.modelRepository.claimName=", "claimName is required"),
        ("serve.maxReplicas=9", "exceeds ray.worker.maxReplicas"),
        ("serve.minReplicas=5", "serve.minReplicas exceeds"),
        ("triton.resources.limits.nvidia\\.com/gpu=2", "exactly one nvidia.com/gpu"),
        (
            "ray.worker.terminationGracePeriodSeconds=30",
            "must exceed Triton's --exit-timeout-secs",
        ),
        (
            "triton.modelRepository.mountPath=/elsewhere",
            "never mention triton.modelRepository.mountPath",
        ),
        ("python.pip={grpcio}", "must pin tritonclient=="),
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
