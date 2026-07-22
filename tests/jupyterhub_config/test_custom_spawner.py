"""Tests for extraFiles/custom-spawner.py — CILogon auth gating and hooks."""

import os

import pytest
from hub_helpers import load_snippet
from oauthenticator.cilogon import CILogonOAuthenticator
from tornado import web

PURDUE_LIST = "/etc/secrets/af-auth-purdue/userlist"
CERN_LIST = "/etc/secrets/af-auth-cern/userlist"


@pytest.fixture
def spawner_ns(monkeypatch, tmp_path):
    """Load the snippet with userlist files redirected to tmp files."""
    purdue_file = tmp_path / "purdue-userlist"
    cern_file = tmp_path / "cern-userlist"
    purdue_file.write_text("alice\nbob\n")
    cern_file.write_text("carol\n")

    real_open = open
    redirect = {PURDUE_LIST: purdue_file, CERN_LIST: cern_file}

    def fake_open(path, *args, **kwargs):
        return real_open(redirect.get(path, path), *args, **kwargs)

    ns = load_snippet(
        "custom-spawner.py", monkeypatch, extra_globals={"open": fake_open}
    )
    ns["_userlists"] = {"purdue": purdue_file, "cern": cern_file}
    return ns


def make_authenticator(ns, monkeypatch, eppn):
    """Instantiate the authenticator with the CILogon upstream mocked."""
    auth = ns["PurdueCILogonOAuthenticator"]()

    async def fake_super_authenticate(self, handler, data=None):
        return {"name": "raw", "auth_state": {"cilogon_user": {"eppn": eppn}}}

    monkeypatch.setattr(CILogonOAuthenticator, "authenticate", fake_super_authenticate)
    return auth


# ── authenticate: domain mapping + userlist gates ─────────────────────────────


async def test_purdue_user_in_list(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns, monkeypatch, "alice@purdue.edu")
    ret = await auth.authenticate(None)
    assert ret["name"] == "alice"
    assert ret["domain"] == "purdue.edu"
    assert os.environ["USERNAME"] == "alice"


async def test_purdue_user_not_in_list_is_denied(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns, monkeypatch, "mallory@purdue.edu")
    with pytest.raises(web.HTTPError) as err:
        await auth.authenticate(None)
    assert err.value.status_code == 500


async def test_cern_user_gets_suffix(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns, monkeypatch, "carol@cern.ch")
    ret = await auth.authenticate(None)
    assert ret["name"] == "carol-cern"
    assert ret["domain"] == "cern.ch"
    assert os.environ["USERNAME"] == "carol-cern"


async def test_cern_user_not_in_list_is_denied(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns, monkeypatch, "mallory@cern.ch")
    with pytest.raises(web.HTTPError) as err:
        await auth.authenticate(None)
    assert err.value.status_code == 500


async def test_fnal_user_needs_no_list(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns, monkeypatch, "dave@fnal.gov")
    ret = await auth.authenticate(None)
    assert ret["name"] == "dave-fnal"
    assert ret["domain"] == "fnal.gov"


async def test_unknown_domain_is_denied(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns, monkeypatch, "eve@evil.example")
    with pytest.raises(web.HTTPError) as err:
        await auth.authenticate(None)
    assert err.value.status_code == 500


async def test_userlist_requires_exact_newline_terminated_line(spawner_ns, monkeypatch):
    # Matching is `f"{username}\\n" in readlines()` — no strip.
    spawner_ns["_userlists"]["purdue"].write_text("  alice  \nbob\n")
    auth = make_authenticator(spawner_ns, monkeypatch, "alice@purdue.edu")
    with pytest.raises(web.HTTPError) as err:
        await auth.authenticate(None)
    assert err.value.status_code == 500


# ── refresh_user ──────────────────────────────────────────────────────────────


async def test_refresh_user_keeps_login_auth_state(spawner_ns):
    # Must stay a no-op: oauthenticator ≥17.2 would otherwise drop name/domain.
    auth = spawner_ns["PurdueCILogonOAuthenticator"]()
    assert await auth.refresh_user(user=None) is True


# ── post_auth_hook ────────────────────────────────────────────────────────────


def test_post_auth_hook_copies_identity_into_auth_state(spawner_ns):
    hook = spawner_ns["passthrough_post_auth_hook"]
    authentication = {"name": "alice", "domain": "purdue.edu", "auth_state": {}}
    out = hook(None, None, authentication)
    assert out["auth_state"]["name"] == "alice"
    assert out["auth_state"]["domain"] == "purdue.edu"


def test_post_auth_hook_creates_auth_state_if_missing(spawner_ns):
    hook = spawner_ns["passthrough_post_auth_hook"]
    out = hook(
        None, None, {"name": "alice", "domain": "purdue.edu", "auth_state": None}
    )
    assert out["auth_state"]["name"] == "alice"


# ── hub config wiring ─────────────────────────────────────────────────────────


def test_config_registers_authenticator_and_hook(spawner_ns):
    c = spawner_ns["c"]
    assert (
        c["JupyterHub"]["authenticator_class"]
        is spawner_ns["PurdueCILogonOAuthenticator"]
    )
    assert (
        c["PurdueCILogonOAuthenticator"]["post_auth_hook"]
        is spawner_ns["passthrough_post_auth_hook"]
    )


def test_dask_gateway_env_set_in_cms_namespace(monkeypatch):
    ns = load_snippet("custom-spawner.py", monkeypatch, namespace="cms")
    env = ns["c"]["KubeSpawner"]["environment"]
    assert "DASK_GATEWAY__ADDRESS" in env
    assert "DASK_GATEWAY__PROXY_ADDRESS" in env


def test_dask_gateway_env_not_set_outside_cms(monkeypatch):
    ns = load_snippet("custom-spawner.py", monkeypatch, namespace="dev")
    env = ns["c"].get("KubeSpawner", {}).get("environment", {})
    assert "DASK_GATEWAY__ADDRESS" not in env
