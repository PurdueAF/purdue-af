"""Tests for extraFiles/ray-train.py: Ray Client in every session signs in
to the Ray Train gateway with that session's own JupyterHub token."""

import types

import yaml
from common import REPO
from hub_helpers import load_snippet

GATEWAY_CONFIG = REPO / "apps" / "ray-train" / "config.yaml"


def session_env(monkeypatch):
    return load_snippet("ray-train.py", monkeypatch)["c"].KubeSpawner.environment


def test_a_session_signs_in_with_its_own_token(monkeypatch):
    env = session_env(monkeypatch)
    assert env["RAY_AUTH_MODE"] == "token"
    spawner = types.SimpleNamespace(api_token="token-of-this-session")
    assert env["RAY_AUTH_TOKEN"](spawner) == "token-of-this-session"


def test_a_local_ray_init_is_left_alone(monkeypatch):
    """RAY_ADDRESS would send every ray.init() in a session to the gateway."""
    assert "RAY_ADDRESS" not in session_env(monkeypatch)


def test_a_removed_cluster_stays_gone_while_its_clients_retry(monkeypatch):
    """A client still retrying after the gateway forgot its cluster would be
    given a new one."""
    grace = int(session_env(monkeypatch)["RAY_CLIENT_RECONNECT_GRACE_PERIOD"])
    gone = yaml.safe_load(GATEWAY_CONFIG.read_text())["goneSeconds"]
    assert 0 < grace < gone
