"""Tests for apps/ray-train — the gateway to each user's own Ray cluster.

The wiring that only fails at deploy time, and what the isolation rests on:
a user's cluster carries no Kubernetes credentials and demands its token,
the gateway can create Secrets but never read them, only sessions reach the
gateway and only the gateway reaches the clusters, and the Hub hands the
gateway exactly the token and scopes it uses.
"""

import tomllib
from pathlib import Path

import yaml
from common import REPO, load_script

APP = REPO / "apps" / "ray-train"
HUB_VALUES = REPO / "apps" / "jupyterhub" / "jupyterhub" / "values.yaml"
OPERATOR_VALUES = REPO / "apps" / "ray" / "operator" / "values.yaml"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"
CORE = REPO / "deploy" / "core-production" / "kustomization.yaml"
GLOBAL_ENV = REPO / "pixi" / "global" / "pixi.toml"


def load(path):
    return yaml.safe_load(path.read_text())


def load_all(path):
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def by_kind(path, kind):
    (doc,) = [d for d in load_all(path) if d["kind"] == kind]
    return doc


def gateway_pod():
    return load(APP / "deployment.yaml")["spec"]["template"]


def cluster_pod():
    return load(APP / "raycluster.yaml")["spec"]["headGroupSpec"]["template"]


def test_flux_deploys_the_gateway():
    kustomization = load(EXPERIMENTAL)
    for name in ("rbac", "deployment", "service", "networkpolicy"):
        assert f"../../apps/ray-train/{name}.yaml" in kustomization["resources"]
    generators = {g["name"]: g for g in kustomization["configMapGenerator"]}
    generator = generators["ray-train-gateway"]
    assert generator["files"] == [
        "../../apps/ray-train/gateway.py",
        "../../apps/ray-train/raycluster.yaml",
    ]
    annotations = generator["options"]["annotations"]
    assert annotations["kustomize.toolkit.fluxcd.io/substitute"] == "disabled"


def test_the_gateway_runs_the_files_it_is_given():
    gateway = load_script(APP / "gateway.py", "ray_train_gateway_manifests")
    pod = gateway_pod()["spec"]
    (container,) = pod["containers"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    (app,) = [m for m in container["volumeMounts"] if m["name"] == "app"]
    assert volumes["app"]["configMap"]["name"] == "ray-train-gateway"
    # kubelet never updates a subPath mount, so the gateway would never see new code.
    assert "subPath" not in app
    assert container["command"] == ["python", f"{app['mountPath']}/gateway.py"]
    assert gateway.TEMPLATE == Path(app["mountPath"]) / "raycluster.yaml"
    service = load(APP / "service.yaml")
    assert (
        service["spec"]["selector"].items()
        <= gateway_pod()["metadata"]["labels"].items()
    )
    ports = {p["targetPort"]: p["port"] for p in service["spec"]["ports"]}
    container_ports = {p["name"]: p["containerPort"] for p in container["ports"]}
    assert ports == container_ports
    assert sorted(ports.values()) == [gateway.DASHBOARD_PORT, gateway.CLIENT_PORT]


def test_the_hub_gives_the_gateway_its_token_and_scopes():
    """z2jh keeps a token-only service's token in the `hub` Secret. The key is
    mounted rather than put in the environment, so the gateway sees it appear
    once the Hub registers the service, without a restart."""
    gateway = load_script(APP / "gateway.py", "ray_train_gateway_manifests")
    hub = load(HUB_VALUES)["hub"]
    assert "ray-train-gateway" in hub["services"]
    role = hub["loadRoles"]["ray-train-gateway"]
    assert role["services"] == ["ray-train-gateway"]
    assert set(role["scopes"]) == {"read:servers", "admin:server_state"}
    pod = gateway_pod()["spec"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["hub-token"]["secret"] == {
        "secretName": "hub",
        "optional": True,
        "items": [{"key": "hub.services.ray-train-gateway.apiToken", "path": "token"}],
    }
    (container,) = pod["containers"]
    (mount,) = [m for m in container["volumeMounts"] if m["name"] == "hub-token"]
    assert gateway.SERVICE_TOKEN_FILE == Path(mount["mountPath"]) / "token"
    assert "JUPYTERHUB_API_TOKEN" not in {e["name"] for e in container["env"]}
    assert (
        gateway_pod()["metadata"]["labels"]["hub.jupyter.org/network-access-hub"]
        == "true"
    )


def test_the_gateway_reads_the_hubs_session_pod_names():
    gateway = load_script(APP / "gateway.py", "ray_train_gateway_manifests")
    template = load(HUB_VALUES)["singleuser"]["podNameTemplate"]
    match = gateway.SESSION_POD.fullmatch(template.format(userid=42))
    assert match and match[1] == "42"


def test_the_hub_points_sessions_at_the_gateway():
    generators = {g["name"]: g for g in load(CORE)["configMapGenerator"]}
    assert (
        "04-ray-train.py=../../apps/jupyterhub/jupyterhub/extraFiles/ray-train.py"
        in generators["jupyterhub-extra-config"]["files"]
    )


def test_user_clusters_hold_no_kubernetes_credentials():
    """User code runs in every pod of a cluster. KubeRay's autoscaler Role
    would let it read every pod in the namespace and patch every RayCluster."""
    spec = load(APP / "raycluster.yaml")["spec"]
    assert not spec.get("enableInTreeAutoscaling")
    (group,) = spec["workerGroupSpecs"]
    for template in (spec["headGroupSpec"]["template"], group["template"]):
        assert template["spec"]["automountServiceAccountToken"] is False
        assert "serviceAccountName" not in template["spec"]


def test_user_code_gains_no_privileges():
    """The image's sudo, like any setuid binary, is no way up for user code."""
    spec = load(APP / "raycluster.yaml")["spec"]
    (group,) = spec["workerGroupSpecs"]
    for template in (spec["headGroupSpec"]["template"], group["template"]):
        assert template["spec"]["securityContext"]["runAsNonRoot"] is True
        (container,) = template["spec"]["containers"]
        assert container["securityContext"] == {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        }


def test_a_clusters_pods_reach_each_other_and_no_one_elses():
    """KubeRay writes each cluster a NetworkPolicy admitting its own pods;
    the operator does so only with the feature gate on."""
    spec = load(APP / "raycluster.yaml")["spec"]
    assert spec["networkPolicy"] == {"mode": "DenyAllIngress"}
    gates = {g["name"]: g["enabled"] for g in load(OPERATOR_VALUES)["featureGates"]}
    assert gates["RayClusterNetworkPolicy"] is True


def test_user_clusters_demand_their_token():
    spec = load(APP / "raycluster.yaml")["spec"]
    assert spec["authOptions"]["mode"] == "token"
    # KubeRay refuses token authentication below Ray 2.52.
    major, minor = (int(p) for p in spec["rayVersion"].split(".")[:2])
    assert (major, minor) >= (2, 52)


def test_user_clusters_can_run_the_images_python():
    """The Ray image keeps its Python under /home/ray, mode 750 for group 100;
    a user's UID and GID alone cannot enter it."""
    assert 100 in cluster_pod()["spec"]["securityContext"]["supplementalGroups"]


def test_the_head_runs_no_tasks_and_holds_no_gpu():
    """Ray's advice: the head runs the GCS, the dashboard and the drivers."""
    head = load(APP / "raycluster.yaml")["spec"]["headGroupSpec"]
    assert head["rayStartParams"]["num-cpus"] == "0"
    (container,) = head["template"]["spec"]["containers"]
    for amounts in container["resources"].values():
        assert "nvidia.com/gpu" not in amounts


def test_a_worker_is_the_heads_pod_with_a_gpu():
    spec = load(APP / "raycluster.yaml")["spec"]
    (group,) = spec["workerGroupSpecs"]
    head, worker = spec["headGroupSpec"]["template"], group["template"]
    (head_container,) = head["spec"]["containers"]
    (worker_container,) = worker["spec"]["containers"]
    assert worker["metadata"] == head["metadata"]
    assert {k: v for k, v in worker["spec"].items() if k != "containers"} == {
        k: v for k, v in head["spec"].items() if k != "containers"
    }
    same = ("image", "securityContext", "env", "volumeMounts")
    assert {k: worker_container[k] for k in same} == {
        k: head_container[k] for k in same
    }
    resources = worker_container["resources"]
    assert (
        resources["limits"]["nvidia.com/gpu"]
        == resources["requests"]["nvidia.com/gpu"]
        == 1
    )


def test_the_gateway_never_reads_a_secret():
    role = by_kind(APP / "rbac.yaml", "Role")
    (secrets,) = [rule for rule in role["rules"] if "secrets" in rule["resources"]]
    assert secrets["verbs"] == ["create"]
    binding = by_kind(APP / "rbac.yaml", "RoleBinding")
    assert binding["subjects"][0]["name"] == gateway_pod()["spec"]["serviceAccountName"]


def test_only_sessions_reach_the_gateway_and_only_the_gateway_reaches_clusters():
    gw = load_script(APP / "gateway.py", "ray_train_gateway_manifests")
    policies = {
        p["metadata"]["name"]: p["spec"] for p in load_all(APP / "networkpolicy.yaml")
    }
    ports = [p["port"] for p in load(APP / "service.yaml")["spec"]["ports"]]
    gateway_labels = gateway_pod()["metadata"]["labels"]
    cluster_labels = cluster_pod()["metadata"]["labels"]

    gateway = policies["ray-train-gateway"]
    assert gateway["podSelector"]["matchLabels"].items() <= gateway_labels.items()
    (rule,) = gateway["ingress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": port} for port in ports]
    assert rule["from"] == [
        {
            "podSelector": {
                "matchLabels": {"app": "jupyterhub", "component": "singleuser-server"}
            }
        }
    ]

    clusters = policies["ray-train-clusters"]
    assert clusters["podSelector"]["matchLabels"].items() <= cluster_labels.items()
    (rule,) = clusters["ingress"]
    dashboard = int(gw.head_url("c").rsplit(":", 1)[1])
    assert rule["ports"] == [
        {"protocol": "TCP", "port": gw.CLIENT_PORT},
        {"protocol": "TCP", "port": dashboard},
    ]
    assert rule["from"] == [
        {"podSelector": {"matchLabels": gateway["podSelector"]["matchLabels"]}}
    ]


def test_clusters_default_to_the_environment_pixi_global_sync_keeps():
    gateway = load_script(APP / "gateway.py", "ray_train_gateway_manifests")
    sync = load_script(
        REPO / "apps" / "af-utils" / "pixi-global-sync" / "sync-global-env.py",
        "sync_global_env_for_ray_train",
    )
    assert gateway.DEFAULT_ENV == str(sync.LIVE_DIR / ".pixi" / "envs" / sync.ENV_NAME)


def test_the_global_environment_has_a_ray_a_cluster_can_run():
    """The oldest Ray the cluster template declares, which KubeRay probes for."""
    dependencies = tomllib.loads(GLOBAL_ENV.read_text())["dependencies"]
    declared = load(APP / "raycluster.yaml")["spec"]["rayVersion"]
    assert dependencies["ray-default"] == ">=" + ".".join(declared.split(".")[:2])
    assert "ray-train" in dependencies
