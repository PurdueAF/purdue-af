"""Tests for apps/ray — the supersonic release's Triton, run on Ray.

Two jobs here.

One: the *parity*. The whole point of sonic-ray is that each worker pod runs
the same Triton, with the same arguments and resources, over the same model
repository as `supersonic` — so it serves what supersonic serves. If somebody
retunes supersonic and forgets Ray, these tests say so.

Two: standing in for kubeconform. ray.io has no schema in the CRDs-catalog, so
the RayService is skipped there (see .github/workflows/validate-manifests.sh)
and the wiring inside it — the Serve config, the ConfigMap the app is imported
from, the resource that pairs a replica with a Triton, the labels the Services
select on — is checked here instead.
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


# -- one replica, one Triton ------------------------------------------------


def test_replica_is_pinned_to_a_pod_with_a_triton():
    """The custom resource is what stops a replica landing where no Triton is,
    and what makes "one more replica" mean "one more GPU"."""
    declared = worker_group()["rayStartParams"]["resources"]
    assert "triton" in declared
    claimed = deployment_config()["ray_actor_options"]["resources"]
    assert claimed == {"triton": 1}


def test_replica_range_matches_keda():
    keda = supersonic_values()["keda"]
    autoscaling = deployment_config()["autoscaling_config"]
    assert autoscaling["min_replicas"] == keda["minReplicaCount"]
    assert autoscaling["max_replicas"] == keda["maxReplicaCount"]
    # Serve asks for replicas; the Ray autoscaler has to be able to add pods.
    assert cluster()["enableInTreeAutoscaling"] is True
    assert worker_group()["minReplicas"] == keda["minReplicaCount"]
    assert worker_group()["maxReplicas"] == keda["maxReplicaCount"]


def test_proxy_talks_to_its_own_pods_triton():
    endpoint = serve_app()["runtime_env"]["env_vars"]["TRITON_HTTP"]
    # Anything but loopback would break the one-replica-one-Triton pairing.
    assert "127.0.0.1:8000" in endpoint
    triton_http = {
        p["name"]: p["containerPort"]
        for p in container(worker_pod(), "triton")["ports"]
    }
    assert triton_http["http"] == 8000


# -- the Serve app gets to the replicas ------------------------------------


def test_serve_app_is_mounted_and_importable():
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

    # The controller imports the app on the head; replicas import it on the
    # workers. Both need the mount, and both need it on the path.
    for pod, name in ((head_pod(), "ray-head"), (worker_pod(), "ray-worker")):
        volume = next(v for v in pod["volumes"] if v["name"] == "serve-app")
        assert volume["configMap"]["name"] == "sonic-ray-serve-app"
        assert (
            env_of(pod, name, "PYTHONPATH")
            == mount_of(pod, name, "serve-app")["mountPath"]
        )


def test_health_check_tolerates_a_cold_triton():
    """Triton binds its HTTP endpoint only after the initial --load-model pass.
    A health check that failed during that window would have Serve restart the
    replica on a loop while the repository was still loading."""
    source = SERVE_APP.read_text()
    assert "STARTUP_GRACE_S" in source
    assert "_was_healthy" in source
    # /v2/health/ready stays false for as long as a model is loading; liveness
    # is the question Serve should be asking.
    assert "/v2/health/live" in source


def test_proxy_forwards_rather_than_reimplements():
    """Parity depends on the proxy being transparent: no endpoint allowlist, no
    body rewriting, and the binary tensor extension's header left alone."""
    source = SERVE_APP.read_text()
    assert "request.method" in source and "request.url.path" in source
    assert "await request.body()" in source
    # Hop-by-hop headers must be stripped, everything else forwarded.
    assert "transfer-encoding" in source and "content-length" in source


# -- services ---------------------------------------------------------------


def test_grpc_entry_point_is_a_private_pool_load_balancer():
    """Envoy's job: one address on the private pool, fronting every Triton."""
    svc = services()["sonic-ray-triton"]
    assert svc["spec"]["type"] == "LoadBalancer"
    annotations = svc["metadata"]["annotations"]
    assert annotations["metallb.universe.tf/address-pool"] == "geddes-private-pool"
    ports = {p["name"]: p["port"] for p in svc["spec"]["ports"]}
    # gRPC is what CMSSW's SONIC clients speak.
    assert ports["grpc"] == 8001
    assert ports["http"] == 8000
    # The head runs no Triton.
    assert svc["spec"]["selector"]["app.kubernetes.io/component"] == "worker"


def test_head_service_is_the_serve_entry_point():
    head = cluster()["headGroupSpec"]
    assert head["serviceType"] == "LoadBalancer"
    annotations = head["headService"]["metadata"]["annotations"]
    assert annotations["metallb.universe.tf/address-pool"] == "geddes-private-pool"
    ports = {
        p["name"]: p["containerPort"]
        for p in container(head_pod(), "ray-head")["ports"]
    }
    # Without an explicit serve port the head Service cannot reach the proxy.
    assert ports["serve"] == 8000
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
