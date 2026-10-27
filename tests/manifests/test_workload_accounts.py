"""Workload accounts (apps/dask-gateway/README.md#workload-accounts): each
declaration is in the af-accounts ConfigMap of the Flux root that deploys both
gateways, beside the volume and the token Secret of its app."""

import yaml
from common import REPO

APPS = REPO / "apps"
FLUX = REPO / "flux"


def load_all(path):
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def listed(path):
    """How a Flux root names a file of the repository."""
    return f"../../{path.relative_to(REPO)}"


def declarations():
    """Each apps/<app>/account.yaml, and the account it declares."""
    return {
        path: yaml.safe_load(path.read_text()) for path in APPS.rglob("account.yaml")
    }


def accounts_root():
    """The kustomization of the one Flux root that generates af-accounts, and that generator."""
    found = []
    for path in sorted(FLUX.glob("*/kustomization.yaml")):
        kustomization = yaml.safe_load(path.read_text())
        for generator in kustomization.get("configMapGenerator") or []:
            if generator["name"] == "af-accounts":
                found.append((kustomization, generator))
    (root,) = found
    return root


def test_af_accounts_holds_every_declaration_under_its_name():
    _, generator = accounts_root()
    assert declarations()
    assert sorted(generator["files"]) == sorted(
        f"{declared['name']}.yaml={listed(path)}"
        for path, declared in declarations().items()
    )
    annotations = generator["options"]["annotations"]
    assert annotations["kustomize.toolkit.fluxcd.io/substitute"] == "disabled"


def test_the_root_of_the_accounts_deploys_both_gateways():
    kustomization, _ = accounts_root()
    resources = kustomization["resources"]
    assert listed(APPS / "dask-gateway" / "helmrelease.yaml") in resources
    assert listed(APPS / "ray-train" / "deployment.yaml") in resources


def test_each_account_has_its_volume_and_its_encrypted_token_beside_it():
    kustomization, _ = accounts_root()
    resources = kustomization["resources"]
    for path, declared in declarations().items():
        name = declared["name"]
        manifests = [
            (manifest, doc)
            for manifest in sorted(path.parent.glob("*.yaml"))
            if manifest != path
            for doc in load_all(manifest)
        ]
        (claim,) = [
            manifest
            for manifest, doc in manifests
            if doc.get("kind") == "PersistentVolumeClaim"
            and doc["metadata"]["name"] == name
        ]
        assert listed(claim) in resources
        ((secret_file, secret),) = [
            (manifest, doc)
            for manifest, doc in manifests
            if doc.get("kind") == "Secret"
            and doc["metadata"]["name"] == f"{name}-account"
        ]
        assert listed(secret_file) in resources
        keys = {*(secret.get("stringData") or {}), *(secret.get("data") or {})}
        assert keys == {"token"}
        assert secret["sops"]["mac"]
        # A boolean, so that a failure never echoes the token.
        encrypted = str(
            (secret.get("stringData") or secret.get("data"))["token"]
        ).startswith("ENC[")
        assert encrypted, f"{secret_file}: the token is not encrypted"
