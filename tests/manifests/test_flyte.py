"""Tests for apps/flyte wiring."""

from common import EXPERIMENTAL, REPO, load_all, load_yaml

FLYTE = REPO / "apps" / "flyte"


def test_flux_deploys_flyte():
    resources = load_yaml(EXPERIMENTAL)["resources"]
    for name in (
        "helmrepo",
        "helmrelease",
        "postgres",
        "minio",
        "podtemplate",
        "secret-console-auth",
    ):
        assert f"../../apps/flyte/{name}.yaml" in resources


def test_task_pods_use_the_pod_template_in_cms():
    values = load_yaml(FLYTE / "values.yaml")
    template = load_yaml(FLYTE / "podtemplate.yaml")
    k8s = values["configuration"]["inline"]["plugins"]["k8s"]
    assert k8s["default-pod-template-name"] == template["metadata"]["name"]
    assert (
        values["flyte-core-components"]["actions"]["kubernetes"]["namespace"] == "cms"
    )
    mounts = template["template"]["spec"]["containers"][0]["volumeMounts"]
    assert {m["mountPath"] for m in mounts} == {"/work"}


def test_backends_match_their_services():
    values = load_yaml(FLYTE / "values.yaml")
    services = {
        d["metadata"]["name"]
        for d in load_all(FLYTE / "postgres.yaml") + load_all(FLYTE / "minio.yaml")
        if d["kind"] == "Service"
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


def test_console_is_served_behind_basic_auth_on_one_host():
    values = load_yaml(FLYTE / "values.yaml")
    assert values["console"]["image"]["tag"] != "latest"
    ingress = values["ingress"]
    assert ingress["create"] == "${enable_ingresses}"
    assert ingress["tls"][0]["hosts"] == [ingress["host"]]
    annotations = ingress["commonAnnotations"]
    assert annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
    secret_name = annotations["nginx.ingress.kubernetes.io/auth-secret"]

    (secret,) = load_all(FLYTE / "secret-console-auth.yaml")
    assert secret["metadata"]["name"] == secret_name
