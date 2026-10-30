"""Tests for apps/interlink/nrp/token-renewal/renew.py: the kubeconfig it writes."""

import json
import os
import subprocess
import sys

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
