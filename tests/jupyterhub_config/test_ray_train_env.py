"""Tests for extraFiles/ray-train.py: every session's Ray Jobs CLI reaches the
Ray Train gateway, signed in with that session's own JupyterHub token."""

import types

import yaml
from common import REPO
from hub_helpers import load_snippet

SERVICE = REPO / "apps" / "ray-train" / "service.yaml"


def session_env(monkeypatch):
    return load_snippet("ray-train.py", monkeypatch)["c"].KubeSpawner.environment


def test_sessions_address_the_gateway_service(monkeypatch):
    service = yaml.safe_load(SERVICE.read_text())
    port = service["spec"]["ports"][0]["port"]
    name = service["metadata"]["name"]
    env = session_env(monkeypatch)
    assert (
        env["RAY_API_SERVER_ADDRESS"] == f"http://{name}.cms.svc.cluster.local:{port}"
    )


def test_a_session_signs_in_with_its_own_token(monkeypatch):
    env = session_env(monkeypatch)
    assert env["RAY_AUTH_MODE"] == "token"
    spawner = types.SimpleNamespace(api_token="token-of-this-session")
    assert env["RAY_AUTH_TOKEN"](spawner) == "token-of-this-session"


def test_a_local_ray_init_is_left_alone(monkeypatch):
    """RAY_ADDRESS would send ray.init() in a notebook to the gateway."""
    assert "RAY_ADDRESS" not in session_env(monkeypatch)
