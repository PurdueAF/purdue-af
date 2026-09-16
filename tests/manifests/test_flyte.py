"""Tests for apps/flyte and workflows/integration-challenge wiring."""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
FLYTE = REPO / "apps" / "flyte"
WORKFLOW = REPO / "workflows" / "integration-challenge"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"


def test_flux_deploys_flyte():
    kustomization = yaml.safe_load(EXPERIMENTAL.read_text())
    for name in ("helmrepo", "helmrelease", "postgres", "minio", "podtemplate"):
        assert f"../../apps/flyte/{name}.yaml" in kustomization["resources"]
    generators = {g["name"]: g for g in kustomization["configMapGenerator"]}
    assert generators["flyte-config"]["files"] == [
        "values.yaml=../../apps/flyte/values.yaml"
    ]


def test_task_pods_use_the_pod_template_in_cms():
    values = yaml.safe_load((FLYTE / "values.yaml").read_text())
    template = yaml.safe_load((FLYTE / "podtemplate.yaml").read_text())
    k8s = values["configuration"]["inline"]["plugins"]["k8s"]
    assert k8s["default-pod-template-name"] == template["metadata"]["name"]
    assert (
        values["flyte-core-components"]["actions"]["kubernetes"]["namespace"] == "cms"
    )
    mounts = template["template"]["spec"]["containers"][0]["volumeMounts"]
    assert {m["mountPath"] for m in mounts} == {"/work"}


def test_backends_match_their_services():
    values = yaml.safe_load((FLYTE / "values.yaml").read_text())
    postgres = [d for d in yaml.safe_load_all((FLYTE / "postgres.yaml").read_text())]
    minio = [d for d in yaml.safe_load_all((FLYTE / "minio.yaml").read_text())]
    services = {
        d["metadata"]["name"] for d in postgres + minio if d["kind"] == "Service"
    }
    assert values["configuration"]["database"]["postgres"]["host"] in services
    endpoint = values["configuration"]["storage"]["providerConfig"]["s3"]["endpoint"]
    assert endpoint.split("//")[1].split(".")[0] in services
    env = {
        k: v
        for e in values["configuration"]["inline"]["plugins"]["k8s"]["default-env-vars"]
        for k, v in e.items()
    }
    assert env["FLYTE_AWS_ENDPOINT"] == endpoint


def test_workflow_targets_the_same_control_plane():
    config = yaml.safe_load((WORKFLOW / "config.yaml").read_text())
    values = yaml.safe_load((FLYTE / "values.yaml").read_text())
    assert config["admin"]["endpoint"].startswith(
        f"dns:///{values['fullnameOverride']}-http.cms"
    )
    workflow = (WORKFLOW / "workflow.py").read_text()
    assert (
        "PIXI_PROJECT" in workflow and 'cache=flyte.Cache(behavior="auto"' in workflow
    )


def test_console_is_served_behind_basic_auth_on_one_host():
    """The console calls the API on its own origin, and the API has no auth of
    its own: the ingress must carry both and gate both."""
    values = yaml.safe_load((FLYTE / "values.yaml").read_text())
    assert values["console"]["replicaCount"] == 1
    assert values["console"]["image"]["tag"] != "latest"
    ingress = values["ingress"]
    assert ingress["create"] == "${enable_ingresses}"
    assert ingress["host"] == "flyte-cms.geddes.rcac.purdue.edu"
    assert ingress["tls"][0]["hosts"] == [ingress["host"]]
    annotations = ingress["commonAnnotations"]
    assert annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
    secret_name = annotations["nginx.ingress.kubernetes.io/auth-secret"]

    (secret,) = [
        d
        for d in yaml.safe_load_all((FLYTE / "secret-console-auth.yaml").read_text())
        if d
    ]
    assert secret["metadata"]["name"] == secret_name
    assert secret["stringData"]["auth"].startswith("ENC[AES256_GCM,")
    assert "$2y$" not in (FLYTE / "secret-console-auth.yaml").read_text()
    kustomization = yaml.safe_load(EXPERIMENTAL.read_text())
    assert "../../apps/flyte/secret-console-auth.yaml" in kustomization["resources"]
