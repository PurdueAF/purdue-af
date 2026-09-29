"""Tests for extraFiles/ray-train.py: Ray Client in every session signs in
to the Ray Train gateway with that session's own JupyterHub token."""

import types

from hub_helpers import load_snippet


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
