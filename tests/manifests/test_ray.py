"""Tests for apps/ray — the supersonic release's Triton, run on Ray.

Two jobs here.

One: the *parity*. The whole point of sonic-ray is that each worker pod runs
the same Triton, with the same arguments and resources, over the same model
repository as `supersonic` — so it serves what supersonic serves. If somebody
retunes supersonic and forgets Ray, these tests say so. Autoscaling is
deliberately *not* mirrored: there is no KEDA and no Prometheus in this loop.

Two: standing in for kubeconform. ray.io has no schema in the CRDs-catalog, so
the RayService is skipped there (see .github/workflows/validate-manifests.sh)
and the wiring inside it — the Serve config, the ConfigMap the controller is
imported from, the custom resources the scaling policy is written in, the
labels the Services select on — is checked here instead.
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


def load_all(path):
    return list(yaml.safe_load_all(path.read_text()))


def rayservice():
    return load(RAY / "sonic-ray" / "rayservice.yaml")


def cluster():
    return rayservice()["spec"]["rayClusterConfig"]


def head_pod():
    return cluster()["headGroupSpec"]["template"]["spec"]


def worker_group():
    groups = cluster()["workerGroupSpecs"]
    assert len(groups) == 1, "one GPU group is the mirror; adding more needs a rethink"
    return groups[0]


def worker_pod():
    return worker_group()["template"]["spec"]


def serve_app():
    apps = yaml.safe_load(rayservice()["spec"]["serveConfigV2"])["applications"]
    assert len(apps) == 1
    return apps[0]


def deployment_config():
    deployments = serve_app()["deployments"]
    assert len(deployments) == 1
    return deployments[0]


def supersonic_values():
    return load(SUPERSONIC)


def triton_values():
    return supersonic_values()["triton"]


def container(pod, name):
    return next(c for c in pod["containers"] if c["name"] == name)


def env_of(pod, container_name, key):
    env = container(pod, container_name).get("env", [])
    return next(e["value"] for e in env if e["name"] == key)


def mount_of(pod, container_name, volume):
    return next(
        m for m in container(pod, container_name)["volumeMounts"] if m["name"] == volume
    )


def services():
    return {
        svc["metadata"]["name"]: svc
        for path in ("triton-service.yaml", "metrics-services.yaml")
        for svc in load_all(RAY / "sonic-ray" / path)
    }


# -- Flux wiring -----------------------------------------------------------


def test_flux_deploys_everything():
    text = EXPERIMENTAL.read_text()
    for resource in (
        "../../apps/ray/helmrepo.yaml",
        "../../apps/ray/operator/helmrelease.yaml",
        "../../apps/ray/sonic-ray/rayservice.yaml",
        "../../apps/ray/sonic-ray/triton-service.yaml",
        "../../apps/ray/sonic-ray/metrics-services.yaml",
    ):
        assert resource in text
        for line in text.splitlines():
            if resource in line:
                assert not line.strip().startswith("#"), line


def test_operator_values_reach_the_release():
    generated = {cm["name"]: cm for cm in load(EXPERIMENTAL)["configMapGenerator"]}
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


# -- the Triton in the pod is supersonic's Triton ---------------------------


def test_triton_container_is_the_production_one():
    triton = container(worker_pod(), "triton")
    assert triton["image"] == triton_values()["image"]
    assert triton["resources"] == triton_values()["resources"]


def test_triton_is_started_the_same_way():
    """Copied arguments drift silently; a diff here is the only warning."""
    triton = container(worker_pod(), "triton")
    assert triton["command"] == triton_values()["command"]

    ours = triton["args"][0].split()
    theirs = triton_values()["args"][0].split()
    assert ours == theirs, "Triton arguments no longer match the supersonic release"


def test_triton_serves_grpc_http_and_metrics():
    ports = {
        p["name"]: p["containerPort"]
        for p in container(worker_pod(), "triton")["ports"]
    }
    assert ports == {"http": 8000, "grpc": 8001, "triton-metrics": 8002}


def test_triton_readiness_matches_supersonic():
    probe = container(worker_pod(), "triton")["readinessProbe"]
    assert (
        probe["successThreshold"]
        == triton_values()["readinessProbe"]["successThreshold"]
    )
    assert probe["httpGet"]["path"] == "/v2/health/ready"
    # Loading a large repository must not be mistaken for a broken container.
    assert container(worker_pod(), "triton")["startupProbe"]["failureThreshold"] >= 12


def test_model_repository_is_the_same_claim_read_only():
    volume = next(v for v in worker_pod()["volumes"] if v["name"] == "model-repository")
    claim = volume["persistentVolumeClaim"]
    repository = triton_values()["modelRepository"]
    assert claim["claimName"] == repository["pvc"]["claimName"]
    # Triton only reads it; the model manager owns the writes.
    assert claim["readOnly"] is True
    mount = mount_of(worker_pod(), "triton", "model-repository")
    assert mount["mountPath"] == repository["mountPath"]
    assert mount["readOnly"] is True


def test_pods_land_where_supersonic_pods_land():
    for pod in (head_pod(), worker_pod()):
        assert pod["nodeSelector"] == triton_values()["nodeSelector"]
        assert pod["tolerations"] == triton_values()["tolerations"]
    # The head proxies; it holds no Triton and no replicas.
    assert cluster()["headGroupSpec"]["rayStartParams"]["num-cpus"] == "0"
    assert [c["name"] for c in head_pod()["containers"]] == ["ray-head"]


# -- autoscaling is Ray's, and nothing else's ------------------------------


def test_no_keda_resources():
    """Scaling here is the Ray autoscaler's job. A ScaledObject would mean two
    controllers fighting over the same worker group — and KEDA could not drive
    it anyway: RayCluster exposes no scale subresource."""
    for path in sorted(RAY.rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict):
                continue
            assert doc.get("kind") != "ScaledObject", path
            assert "keda.sh" not in str(doc.get("apiVersion", "")), path


def test_controller_drives_the_ray_autoscaler():
    """request_resources() is the Ray-native "make room for this" API, and the
    triton resource is what makes a bundle mean a GPU pod with a Triton in it."""
    source = SERVE_APP.read_text()
    assert "from ray.autoscaler.sdk import request_resources" in source
    assert "nv_inference_pending_request_count" in source
    assert cluster()["enableInTreeAutoscaling"] is True

    # Demand is expressed in the resource the workers advertise.
    assert "triton" in worker_group()["rayStartParams"]["resources"]


def test_controller_runs_on_the_head_and_holds_no_gpu():
    """A controller that landed on a worker would pin a GPU pod and stop the
    autoscaler ever reclaiming it."""
    options = deployment_config()["ray_actor_options"]
    assert options["num_cpus"] == 0
    assert options["resources"] == {"controller": 1}
    assert "num_gpus" not in options
    # Only the head advertises it.
    assert "controller" in cluster()["headGroupSpec"]["rayStartParams"]["resources"]
    assert "controller" not in worker_group()["rayStartParams"]["resources"]
    assert deployment_config()["num_replicas"] == 1


def test_workers_run_no_ray_work():
    """The autoscaler reclaims idle nodes. An actor on a worker — a Serve
    replica, or an HTTP proxy — would make every worker permanently busy."""
    serve_config = yaml.safe_load(rayservice()["spec"]["serveConfigV2"])
    assert serve_config["proxy_location"] == "HeadOnly"
    # No app to import, so nothing can be scheduled there by accident either.
    ray_worker = container(worker_pod(), "ray-worker")
    assert "volumeMounts" in ray_worker
    assert all(m["name"] != "serve-app" for m in ray_worker["volumeMounts"])


def test_scaling_policy_is_declared_where_it_can_be_retuned():
    """user_config reaches reconfigure() without restarting the replica."""
    policy = deployment_config()["user_config"]
    assert policy["min_servers"] >= 1
    assert policy["max_servers"] > policy["min_servers"]
    assert policy["target_pending_per_server"] > 0
    # Releasing a pod kills a Triton; a lull must not look like the end.
    assert policy["downscale_delay_s"] >= 60
    assert policy["control_interval_s"] < policy["downscale_delay_s"]

    source = SERVE_APP.read_text()
    assert "def reconfigure" in source
    for key in policy:
        assert key in source, f"user_config key {key} is not read by the controller"


def test_worker_group_bounds_contain_the_policy():
    """The autoscaler cannot honour a request outside the group's own bounds."""
    policy = deployment_config()["user_config"]
    group = worker_group()
    assert group["minReplicas"] <= policy["min_servers"]
    assert group["maxReplicas"] >= policy["max_servers"]


def test_triton_is_given_time_to_drain():
    """Scale-down deletes the pod; Triton's --exit-timeout-secs is worth
    nothing if the kubelet does not wait for it."""
    triton_args = container(worker_pod(), "triton")["args"][0]
    exit_timeout = int(triton_args.split("--exit-timeout-secs=")[1].split()[0])
    assert worker_pod()["terminationGracePeriodSeconds"] > exit_timeout


# -- the controller gets to the head ---------------------------------------


def test_controller_is_mounted_and_importable():
    """import_path resolves only if the ConfigMap lands on PYTHONPATH."""
    generated = {cm["name"]: cm for cm in load(EXPERIMENTAL)["configMapGenerator"]}
    assert generated["sonic-ray-serve-app"]["files"] == [
        "../../apps/ray/sonic-ray/serve/sonic_serve.py"
    ]
    # Python source is not config; Flux must not rewrite ${...} inside it.
    annotations = generated["sonic-ray-serve-app"]["options"]["annotations"]
    assert annotations["kustomize.toolkit.fluxcd.io/substitute"] == "disabled"

    module_name, _, attribute = serve_app()["import_path"].partition(":")
    assert module_name == SERVE_APP.stem
    module = ast.parse(SERVE_APP.read_text())
    assert attribute in {
        target.id
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    # A renamed class silently drops every override in serveConfigV2.
    assert deployment_config()["name"] in {
        n.name for n in module.body if isinstance(n, ast.ClassDef)
    }

    volume = next(v for v in head_pod()["volumes"] if v["name"] == "serve-app")
    assert volume["configMap"]["name"] == "sonic-ray-serve-app"
    assert (
        env_of(head_pod(), "ray-head", "PYTHONPATH")
        == mount_of(head_pod(), "ray-head", "serve-app")["mountPath"]
    )


# -- services ---------------------------------------------------------------


def test_grpc_entry_point_is_a_private_pool_load_balancer():
    """Envoy's job: one address on the private pool, fronting every Triton."""
    svc = services()["sonic-ray-triton"]
    assert svc["spec"]["type"] == "LoadBalancer"
    annotations = svc["metadata"]["annotations"]
    assert annotations["metallb.universe.tf/address-pool"] == "geddes-private-pool"
    ports = {p["name"]: p["port"] for p in svc["spec"]["ports"]}
    # gRPC is what CMSSW's SONIC clients speak, and nothing proxies it.
    assert ports["grpc"] == 8001
    assert ports["http"] == 8000
    # The head runs no Triton.
    assert svc["spec"]["selector"]["app.kubernetes.io/component"] == "worker"


def test_head_is_not_exposed():
    """The dashboard has no auth, and the controller's status endpoint is not
    something to reach from outside. Inference has its own address."""
    head = cluster()["headGroupSpec"]
    assert head["serviceType"] == "ClusterIP"
    assert "headService" not in head
    ports = {
        p["name"]: p["containerPort"]
        for p in container(head_pod(), "ray-head")["ports"]
    }
    assert ports["metrics"] == 8080


def test_metrics_services_are_scraped_and_select_pods_that_exist():
    everything = services()
    labels_by_group = {
        "head": cluster()["headGroupSpec"]["template"]["metadata"]["labels"],
        "worker": worker_group()["template"]["metadata"]["labels"],
    }

    ray_metrics = everything["sonic-ray-metrics"]
    assert ray_metrics["metadata"]["labels"]["scrape_metrics"] == "true"
    assert [p["port"] for p in ray_metrics["spec"]["ports"]] == [8080]
    for labels in labels_by_group.values():
        assert ray_metrics["spec"]["selector"].items() <= labels.items()

    triton_metrics = everything["sonic-ray-triton-metrics"]
    assert triton_metrics["metadata"]["labels"]["scrape_metrics"] == "true"
    assert [p["port"] for p in triton_metrics["spec"]["ports"]] == [8002]
    # Only the workers run Triton; scraping 8002 on the head would just fail.
    assert (
        triton_metrics["spec"]["selector"].items() <= labels_by_group["worker"].items()
    )
    assert (
        not triton_metrics["spec"]["selector"].items()
        <= labels_by_group["head"].items()
    )

    # release=<instance> is how the SuperSONIC dashboards select nv_* series.
    for svc in (ray_metrics, triton_metrics):
        assert svc["metadata"]["labels"]["app.kubernetes.io/instance"] == "sonic-ray"
        # The scrape job hits /metrics on every port of a Service it keeps.
        assert "ray.io/cluster" not in svc["spec"]["selector"]


def test_data_service_is_not_scraped():
    """8000 and 8001 are not metrics endpoints; a scrape_metrics label here
    would point the `pods` job at them."""
    assert "scrape_metrics" not in services()["sonic-ray-triton"]["metadata"]["labels"]
