"""Tests for apps/interlink/nrp/token-renewal/renew.py: the kubeconfig it writes."""

import base64
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import yaml
from common import REPO, load_script
from kubernetes import client, config

renew = load_script(
    REPO / "apps" / "interlink" / "nrp" / "token-renewal" / "renew.py", "renew"
)

KUBECONFIG = {
    "apiVersion": "v1",
    "kind": "Config",
    "clusters": [{"name": "nrp", "cluster": {"server": "https://nrp.example:6443"}}],
    "users": [{"name": "interlink-plugin", "user": {"token": "old-token"}}],
    "contexts": [
        {
            "name": "nrp-interlink-cms",
            "context": {
                "cluster": "nrp",
                "user": "interlink-plugin",
                "namespace": "interlink-cms",
            },
        }
    ],
    "current-context": "nrp-interlink-cms",
}


def test_the_kubeconfig_keeps_its_cluster_and_drops_the_token(tmp_path):
    out = renew.renewed(KUBECONFIG, tmp_path / "token")
    assert out["clusters"] == KUBECONFIG["clusters"]
    assert out["contexts"] == KUBECONFIG["contexts"]
    (user,) = out["users"]
    assert user["name"] == "interlink-plugin"
    assert "old-token" not in json.dumps(out)


def test_its_exec_user_prints_the_token_in_the_file(tmp_path):
    (tmp_path / "token").write_text("new\n")
    (user,) = renew.renewed(KUBECONFIG, tmp_path / "token")["users"]
    run = user["user"]["exec"]
    env = os.environ | {e["name"]: e["value"] for e in run["env"]}
    printed = subprocess.run(
        [sys.executable, *run["args"]], env=env, capture_output=True, check=True
    )
    credential = json.loads(printed.stdout)
    assert credential["apiVersion"] == run["apiVersion"]
    assert credential["status"]["token"] == "new"


def test_a_client_of_it_reads_the_token_again_once_its_credential_expires(
    tmp_path, monkeypatch
):
    (tmp_path / "token").write_text("first")
    path = tmp_path / "kubeconfig.yaml"
    kubeconfig = renew.renewed(KUBECONFIG, tmp_path / "token")
    kubeconfig["users"][0]["user"]["exec"]["command"] = sys.executable
    path.write_text(json.dumps(kubeconfig))
    api = client.CoreV1Api(config.new_client_from_config(str(path)))
    settings = api.api_client.configuration.auth_settings
    assert settings()["BearerToken"]["value"] == "Bearer first"
    (tmp_path / "token").write_text("second")
    monkeypatch.setattr(config.kube_config, "_is_expired", lambda expiry: True)
    assert settings()["BearerToken"]["value"] == "Bearer second"


class FakeApi:
    """NRP's API and the cluster's own, as the renewal calls them."""

    def __init__(self):
        self.calls = []

    def __call__(self, api_client=None):
        return self

    def create_namespaced_service_account_token(self, name, namespace, body):
        self.calls.append(("token", name, namespace, body))
        status = SimpleNamespace(token="minted", expiration_timestamp="2026-12-01")
        return SimpleNamespace(status=status)

    def patch_namespaced_secret(self, name, namespace, body):
        self.calls.append(("secret", name, namespace, body))


def test_it_mints_the_next_token_and_writes_both_keys_into_the_secret(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "kubeconfig.yaml").write_text(yaml.safe_dump(KUBECONFIG))
    (tmp_path / "namespace").write_text("cms\n")
    monkeypatch.setattr(renew, "MOUNT", tmp_path)
    monkeypatch.setattr(renew, "NAMESPACE_FILE", tmp_path / "namespace")
    api = FakeApi()
    opened = []
    monkeypatch.setattr(renew.client, "CoreV1Api", api)
    monkeypatch.setattr(renew.config, "new_client_from_config", opened.append)
    monkeypatch.setattr(renew.config, "load_incluster_config", lambda **_: None)
    renew.main()
    assert opened == [str(tmp_path / "kubeconfig.yaml")]
    (_, account, namespace, request), (_, secret, here, patch) = api.calls
    assert (account, namespace) == ("interlink-plugin", "interlink-cms")
    assert request.spec.expiration_seconds == 30 * 86400
    assert (secret, here) == ("kubeconfig-nrp-interlink", "cms")
    data = {k: base64.b64decode(v).decode() for k, v in patch["data"].items()}
    assert data["token"] == "minted"
    assert yaml.safe_load(data["kubeconfig.yaml"]) == renew.renewed(
        KUBECONFIG, tmp_path / "token"
    )
    assert "until 2026-12-01" in capsys.readouterr().out
