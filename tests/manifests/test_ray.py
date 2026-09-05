"""Tests for apps/ray — the supersonic release's Triton, run on Ray.

Two jobs.

One: *parity*. The point of sonic-ray is that each worker pod runs the same
Triton — image, arguments, resources, readiness, model repository — as
`supersonic`, so it serves what supersonic serves. The values file says so in
a comment; these tests make it true. Autoscaling is deliberately not mirrored.

Two: the properties the Ray-side scaling loop depends on, which no schema can
express: the controller stays on the head and off the GPUs, the workers stay
free of Ray work so the autoscaler can reclaim them, the Services select on
labels KubeRay leaves alone, and the policy the controller is handed is one it
actually reads. Those run against the rendered chart when helm is on PATH
(it is in CI), and against the values and source otherwise.
"""

import ast
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
RAY = REPO / "apps" / "ray"
CHART = RAY / "sonic-ray" / "chart"
CONTROLLER = CHART / "files" / "sonic_serve.py"
VALUES = RAY / "sonic-ray" / "values.yaml"
SUPERSONIC = REPO / "apps" / "sonic" / "supersonic" / "values.yaml"
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
    assert len(groups) == 1, (
        "one server is one bundle of one resource; more groups needs a rethink"
    )
    return groups[0]


def container(pod_template, name):
    return next(c for c in pod_template["spec"]["containers"] if c["name"] == name)


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


# -- the Triton in the pod is supersonic's Triton ---------------------------


def test_triton_values_match_supersonic(values, supersonic):
    ours, theirs = values["triton"], supersonic["triton"]
    assert f"{ours['image']['repository']}:{ours['image']['tag']}" == theirs["image"]
    assert ours["command"] == theirs["command"]
    # Token for token: copied arguments drift silently.
    assert ours["args"][0].split() == theirs["args"][0].split()
    assert ours["resources"] == theirs["resources"]
    assert (
        ours["readinessProbe"]["successThreshold"]
        == theirs["readinessProbe"]["successThreshold"]
    )
    assert (
        ours["modelRepository"]["claimName"]
        == theirs["modelRepository"]["pvc"]["claimName"]
    )
    assert (
        ours["modelRepository"]["mountPath"] == theirs["modelRepository"]["mountPath"]
    )


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


def test_rendered_triton_is_the_one_in_values(worker_group, values):
    """The values are only parity if the template actually uses them."""
    triton = container(worker_group["template"], "triton")
    assert (
        triton["image"]
        == f"{values['triton']['image']['repository']}:{values['triton']['image']['tag']}"
    )
    assert triton["command"] == values["triton"]["command"]
    assert triton["args"] == values["triton"]["args"]
    assert triton["resources"] == values["triton"]["resources"]
    assert triton["readinessProbe"]["successThreshold"] == 3
    ports = {p["name"]: p["containerPort"] for p in triton["ports"]}
    assert ports == {"http": 8000, "grpc": 8001, "triton-metrics": 8002}


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


# -- autoscaling is Ray's, and nothing else's ------------------------------


def test_no_keda_resources():
    """A ScaledObject would be a second controller fighting over the group —
    and could not drive it anyway: RayCluster has no scale subresource."""
    for path in sorted(RAY.rglob("*.yaml")):
        text = path.read_text()
        if "templates" in path.parts:  # Go templates, not YAML until rendered
            assert "ScaledObject" not in text and "keda.sh" not in text, path
            continue
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict):
                assert doc.get("kind") != "ScaledObject", path
                assert "keda.sh" not in str(doc.get("apiVersion", "")), path


def test_controller_drives_the_ray_autoscaler(cluster, worker_group):
    source = CONTROLLER.read_text()
    assert "from ray.autoscaler.sdk import request_resources" in source
    assert "nv_inference_pending_request_count" in source
    assert cluster["enableInTreeAutoscaling"] is True
    # Demand is spoken in the resource the workers advertise, and only they do.
    assert '\\"triton\\": 1' in worker_group["rayStartParams"]["resources"]
    assert "triton" not in cluster["headGroupSpec"]["rayStartParams"]["resources"]


def test_controller_runs_on_the_head_and_holds_no_gpu(cluster, deployment):
    """On a worker it would pin a GPU pod the autoscaler could never reclaim."""
    options = deployment["ray_actor_options"]
    assert options == {"num_cpus": 0, "resources": {"controller": 1}}
    assert deployment["num_replicas"] == 1
    assert (
        '\\"controller\\": 1' in cluster["headGroupSpec"]["rayStartParams"]["resources"]
    )
    assert cluster["headGroupSpec"]["rayStartParams"]["num-cpus"] == "0"


def test_workers_run_no_ray_work(serve_config, worker_group):
    """The autoscaler reclaims idle nodes; any actor on a worker — a Serve
    replica, a proxy — makes it permanently busy."""
    assert serve_config["proxy_location"] == "HeadOnly"
    ray_worker = container(worker_group["template"], "ray-worker")
    assert all(m["name"] != "serve-app" for m in ray_worker["volumeMounts"])
    assert "env" not in ray_worker
    # Ray must not see the GPU either, or it would schedule onto it.
    assert "nvidia.com/gpu" not in ray_worker["resources"]["limits"]


def test_policy_is_the_one_the_controller_reads(deployment, chart_defaults):
    """user_config keys the controller never reads are silently ignored."""
    policy = deployment["user_config"]
    assert policy == chart_defaults["autoscaling"]
    module = ast.parse(CONTROLLER.read_text())
    # `DEFAULTS: dict[str, Any] = {...}` is an annotated assignment.
    defaults = next(
        node
        for node in module.body
        if isinstance(node, ast.AnnAssign) and node.target.id == "DEFAULTS"
    )
    known = {k.value for k in defaults.value.keys}
    assert set(policy) <= known, set(policy) - known


def test_worker_group_bounds_are_the_policy_bounds(deployment, worker_group):
    """Two copies of min/max would drift; the template derives one from the other."""
    policy = deployment["user_config"]
    assert worker_group["minReplicas"] == policy["min_servers"]
    assert worker_group["maxReplicas"] == policy["max_servers"]
    assert worker_group["replicas"] == policy["min_servers"]


def test_scale_down_is_gradual(chart_defaults):
    policy = chart_defaults["autoscaling"]
    assert policy["downscale_step"] == 1
    assert policy["downscale_delay_s"] >= 60
    assert policy["look_back_s"] >= policy["control_interval_s"]


def test_triton_is_given_time_to_drain(worker_group):
    """Scale-down deletes the pod; --exit-timeout-secs is worth nothing if the
    kubelet does not wait for it. (The chart refuses to render otherwise.)"""
    args = container(worker_group["template"], "triton")["args"][0]
    exit_timeout = int(args.split("--exit-timeout-secs=")[1].split()[0])
    assert (
        worker_group["template"]["spec"]["terminationGracePeriodSeconds"] > exit_timeout
    )


def test_controller_reaches_the_head(rendered, head_pod, serve_config):
    """import_path resolves only if the ConfigMap lands on PYTHONPATH."""
    module_name, _, attribute = serve_config["applications"][0][
        "import_path"
    ].partition(":")
    assert module_name == CONTROLLER.stem
    module = ast.parse(CONTROLLER.read_text())
    assert attribute in {
        t.id
        for n in module.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    assert serve_config["applications"][0]["deployments"][0]["name"] in {
        n.name for n in module.body if isinstance(n, ast.ClassDef)
    }

    configmap = rendered[("ConfigMap", "sonic-ray-serve-app")]
    assert configmap["data"]["sonic_serve.py"] == CONTROLLER.read_text()
    volume = next(v for v in head_pod["spec"]["volumes"] if v["name"] == "serve-app")
    assert volume["configMap"]["name"] == "sonic-ray-serve-app"
    head = container(head_pod, "ray-head")
    mount = next(m for m in head["volumeMounts"] if m["name"] == "serve-app")
    assert (
        next(e["value"] for e in head["env"] if e["name"] == "PYTHONPATH")
        == mount["mountPath"]
    )
    # A code change must roll the cluster, or the old controller keeps running.
    assert "checksum/serve-app" in head_pod["metadata"]["annotations"]


# -- services ---------------------------------------------------------------


def test_inference_entry_point_is_the_private_pool(rendered, worker_group):
    """Envoy's job: one address on the private pool, fronting every Triton."""
    svc = rendered[("Service", "sonic-ray-triton")]
    assert svc["spec"]["type"] == "LoadBalancer"
    assert (
        svc["metadata"]["annotations"]["metallb.universe.tf/address-pool"]
        == "geddes-private-pool"
    )
    assert {p["name"]: p["port"] for p in svc["spec"]["ports"]} == {
        "grpc": 8001,
        "http": 8000,
    }
    assert (
        svc["spec"]["selector"].items()
        <= worker_group["template"]["metadata"]["labels"].items()
    )
    assert "scrape_metrics" not in svc["metadata"]["labels"]


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
        # release="sonic-ray" is how the SuperSONIC dashboards select nv_*.
        assert svc["metadata"]["labels"]["app.kubernetes.io/instance"] == "sonic-ray"

    # Every port a metrics Service names exists on the pods it selects.
    worker_ports = {
        p["name"]
        for c in worker_group["template"]["spec"]["containers"]
        for p in c.get("ports", [])
    }
    assert {"metrics", "triton-metrics"} <= worker_ports
