"""Tests for apps/ray — the KubeRay operator every ray.io resource in cms
depends on."""

import yaml
from common import REPO

RAY = REPO / "apps" / "ray"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"


def load(path):
    return yaml.safe_load(path.read_text())


def test_flux_deploys_the_operator():
    resources = load(EXPERIMENTAL)["resources"]
    for resource in (
        "../../apps/ray/helmrepo.yaml",
        "../../apps/ray/operator/helmrelease.yaml",
    ):
        assert resource in resources


def test_values_reach_the_operator():
    """A valuesFrom ConfigMap nobody generates leaves a release on chart defaults."""
    generated = {
        cm["name"]: cm["files"] for cm in load(EXPERIMENTAL)["configMapGenerator"]
    }
    hr = load(RAY / "operator" / "helmrelease.yaml")
    assert [v["name"] for v in hr["spec"]["valuesFrom"]] == ["kuberay-operator-config"]
    assert generated["kuberay-operator-config"] == [
        "values.yaml=../../apps/ray/operator/values.yaml"
    ]


def test_operator_installs_the_crds_in_cms_only():
    operator = load(RAY / "operator" / "helmrelease.yaml")
    assert operator["spec"]["install"]["crds"] == "Create"
    assert operator["spec"]["upgrade"]["crds"] == "CreateReplace"
    # singleNamespaceInstall keeps the watch and the RBAC inside cms.
    assert load(RAY / "operator" / "values.yaml")["singleNamespaceInstall"] is True
