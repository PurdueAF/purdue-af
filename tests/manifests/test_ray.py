"""Tests for apps/ray — the Ray Serve server mirroring the SuperSONIC release.

Two jobs here.

One: the *mirroring*. sonic-ray is worth running because it sits on the same
hardware, at the same size, with the same replica range as `supersonic`, so the
two can be compared. If somebody retunes supersonic and forgets Ray, these
tests say so.

Two: standing in for kubeconform. ray.io has no schema in the CRDs-catalog, so
the RayService is skipped there (see .github/workflows/validate-manifests.sh)
and the wiring inside it — the Serve config, the ConfigMap the app is imported
from, the labels the metrics Service selects on — is checked here instead.
"""

import ast
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
RAY = REPO / "apps" / "ray"
SERVE_APP = RAY / "sonic-ray" / "serve" / "sonic_serve.py"
SUPERSONIC = REPO / "apps" / "sonic" / "supersonic" / "values.yaml"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"
VALIDATOR = REPO / ".github" / "workflows" / "validate-manifests.sh"


def load(path):
    return yaml.safe_load(path.read_text())


def rayservice():
    return load(RAY / "sonic-ray" / "rayservice.yaml")


def cluster():
    return rayservice()["spec"]["rayClusterConfig"]


def head_pod():
    return cluster()["headGroupSpec"]["template"]["spec"]


def worker_group():
    groups = cluster()["workerGroupSpecs"]
    assert len(groups) == 1, "the mirror is one GPU group; adding more needs a rethink"
    return groups[0]


def worker_pod():
    return worker_group()["template"]["spec"]


def serve_config():
    return yaml.safe_load(rayservice()["spec"]["serveConfigV2"])


def serve_app():
    apps = serve_config()["applications"]
    assert len(apps) == 1
    return apps[0]


def deployment_config():
    deployments = serve_app()["deployments"]
    assert len(deployments) == 1
    return deployments[0]


def supersonic_values():
    return load(SUPERSONIC)


def module():
    return ast.parse(SERVE_APP.read_text())


def container(pod, name):
    return next(c for c in pod["containers"] if c["name"] == name)


def env_of(pod, container_name, key):
    env = container(pod, container_name).get("env", [])
    return next(e["value"] for e in env if e["name"] == key)


def mount_of(pod, container_name, volume):
    return next(
        m for m in container(pod, container_name)["volumeMounts"] if m["name"] == volume
    )


# -- Flux wiring -----------------------------------------------------------


def test_flux_deploys_operator_and_service():
    text = EXPERIMENTAL.read_text()
    for resource in (
        "../../apps/ray/helmrepo.yaml",
        "../../apps/ray/operator/helmrelease.yaml",
        "../../apps/ray/sonic-ray/rayservice.yaml",
        "../../apps/ray/sonic-ray/metrics-service.yaml",
    ):
        assert resource in text
        for line in text.splitlines():
            if resource in line:
                assert not line.strip().startswith("#"), line


def test_operator_values_reach_the_release():
    kustomization = load(EXPERIMENTAL)
    generated = {cm["name"]: cm for cm in kustomization["configMapGenerator"]}

    hr = load(RAY / "operator" / "helmrelease.yaml")
    assert [v["name"] for v in hr["spec"]["valuesFrom"]] == ["kuberay-operator-config"]
    assert generated["kuberay-operator-config"]["files"] == [
        "values.yaml=../../apps/ray/operator/values.yaml"
    ]


def test_operator_installs_the_crds_it_is_the_only_source_of():
    operator = load(RAY / "operator" / "helmrelease.yaml")
    assert operator["spec"]["install"]["crds"] == "Create"
    assert operator["spec"]["upgrade"]["crds"] == "CreateReplace"
    # singleNamespaceInstall keeps the watch and the RBAC inside cms.
    assert load(RAY / "operator" / "values.yaml")["singleNamespaceInstall"] is True


def test_rayservice_is_skipped_by_kubeconform_on_purpose():
    """The skip is what these tests compensate for; it must stay deliberate."""
    text = VALIDATOR.read_text()
    assert 'KUBECONFORM_SKIP="RayService"' in text
    assert "-skip" in text
    # An unexplained skip is how validation quietly stops meaning anything.
    assert "ray.io" in text


# -- the Serve app gets to the replicas ------------------------------------


def test_serve_app_is_mounted_and_importable():
    """import_path resolves only if the ConfigMap lands on PYTHONPATH."""
    kustomization = load(EXPERIMENTAL)
    generated = {cm["name"]: cm for cm in kustomization["configMapGenerator"]}
    assert generated["sonic-ray-serve-app"]["files"] == [
        "../../apps/ray/sonic-ray/serve/sonic_serve.py"
    ]
    # Python source is not config; Flux must not rewrite ${...} inside it.
    annotations = generated["sonic-ray-serve-app"]["options"]["annotations"]
    assert annotations["kustomize.toolkit.fluxcd.io/substitute"] == "disabled"

    module_name, _, attribute = serve_app()["import_path"].partition(":")
    assert module_name == SERVE_APP.stem
    assert attribute in {
        target.id
        for node in module().body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    # The controller imports the app on the head; the replicas import it on the
    # workers. Both need the mount, and both need it on the path.
    for pod, container_name in ((head_pod(), "ray-head"), (worker_pod(), "ray-worker")):
        volume = next(v for v in pod["volumes"] if v["name"] == "serve-app")
        assert volume["configMap"]["name"] == "sonic-ray-serve-app"
        assert (
            env_of(pod, container_name, "PYTHONPATH")
            == mount_of(pod, container_name, "serve-app")["mountPath"]
        )


def test_deployment_name_matches_the_class():
    """A renamed class silently drops every override in serveConfigV2."""
    classes = {n.name for n in module().body if isinstance(n, ast.ClassDef)}
    assert deployment_config()["name"] in classes


def test_app_serves_the_kserve_v2_routes_triton_serves():
    """The point of the protocol choice: existing SONIC clients keep working."""
    source = SERVE_APP.read_text()
    for route in (
        '"/v2"',
        '"/v2/health/live"',
        '"/v2/health/ready"',
        '"/v2/repository/index"',
        '"/v2/models/{name}"',
        '"/v2/models/{name}/ready"',
        '"/v2/models/{name}/infer"',
    ):
        assert route in source, route


def test_inference_runtime_is_installed_at_the_replicas():
    """The Ray image has CUDA but no inference runtime; the pins add one."""
    pip = serve_app()["runtime_env"]["pip"]
    assert any(p.startswith("onnxruntime-gpu==") for p in pip), pip
    # ORT dlopen()s cuDNN by soname — without the wheel it falls back to CPU.
    assert any(p.startswith("nvidia-cudnn-cu12") for p in pip), pip


def test_app_reads_the_repository_that_is_mounted():
    repository = serve_app()["runtime_env"]["env_vars"]["MODEL_REPOSITORY"]
    assert (
        mount_of(worker_pod(), "ray-worker", "model-repository")["mountPath"]
        == repository
    )


# -- mirroring supersonic ---------------------------------------------------


def test_gpu_workers_match_triton():
    """Same node pool, same taint, same per-server resources as Triton."""
    triton = supersonic_values()["triton"]
    assert container(worker_pod(), "ray-worker")["resources"] == triton["resources"]
    assert worker_pod()["nodeSelector"] == triton["nodeSelector"]
    assert worker_pod()["tolerations"] == triton["tolerations"]


def test_head_lands_on_the_same_nodes():
    triton = supersonic_values()["triton"]
    assert head_pod()["nodeSelector"] == triton["nodeSelector"]
    assert head_pod()["tolerations"] == triton["tolerations"]
    # The head proxies; it never holds a model. Replicas must stay off it.
    assert cluster()["headGroupSpec"]["rayStartParams"]["num-cpus"] == "0"


def test_replica_range_matches_keda():
    keda = supersonic_values()["keda"]
    autoscaling = deployment_config()["autoscaling_config"]
    assert autoscaling["min_replicas"] == keda["minReplicaCount"]
    assert autoscaling["max_replicas"] == keda["maxReplicaCount"]
    # Serve asks for replicas; the Ray autoscaler has to be able to add pods.
    assert cluster()["enableInTreeAutoscaling"] is True
    assert worker_group()["minReplicas"] == keda["minReplicaCount"]
    assert worker_group()["maxReplicas"] == keda["maxReplicaCount"]


def test_one_replica_per_gpu_like_one_triton_per_pod():
    options = deployment_config()["ray_actor_options"]
    assert options["num_gpus"] == 1
    worker_gpus = container(worker_pod(), "ray-worker")["resources"]["limits"][
        "nvidia.com/gpu"
    ]
    assert worker_gpus == 1


def test_entry_point_is_the_private_pool():
    """Same as envoy: a LoadBalancer on geddes-private-pool, never an ingress."""
    head = cluster()["headGroupSpec"]
    assert head["serviceType"] == "LoadBalancer"
    annotations = head["headService"]["metadata"]["annotations"]
    assert annotations["metallb.universe.tf/address-pool"] == "geddes-private-pool"

    ports = {
        p["name"]: p["containerPort"]
        for p in container(head_pod(), "ray-head")["ports"]
    }
    # Without an explicit serve port the head Service has no way to reach the
    # HTTP proxy, and the LoadBalancer leads nowhere useful.
    assert ports["serve"] == 8000
    assert ports["metrics"] == 8080


def test_model_repository_is_mounted_read_only():
    """Ray reads the repository the model manager owns; it must never write."""
    volume = next(v for v in worker_pod()["volumes"] if v["name"] == "model-repository")
    claim = volume["persistentVolumeClaim"]
    triton_repository = supersonic_values()["triton"]["modelRepository"]
    assert claim["claimName"] == triton_repository["pvc"]["claimName"]
    assert claim["readOnly"] is True
    assert mount_of(worker_pod(), "ray-worker", "model-repository")["readOnly"] is True

    # Ray's own logs still need somewhere to go.
    assert mount_of(worker_pod(), "ray-worker", "log-volume")["mountPath"] == "/tmp/ray"


def test_metrics_service_selects_pods_that_exist():
    """RayService renames the cluster it owns on every upgrade, so the Service
    selects on labels this manifest sets, not on ray.io/cluster."""
    svc = load(RAY / "sonic-ray" / "metrics-service.yaml")
    assert svc["metadata"]["labels"]["scrape_metrics"] == "true"
    assert [p["port"] for p in svc["spec"]["ports"]] == [8080]

    selector = svc["spec"]["selector"]
    assert "ray.io/cluster" not in selector
    for group in (cluster()["headGroupSpec"], worker_group()):
        labels = group["template"]["metadata"]["labels"]
        assert selector.items() <= labels.items()
