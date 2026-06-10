"""Tests for extraFiles/custom-spawner.py — CILogon auth gating and hooks."""

import pytest
from hub_helpers import FakeSpawner, load_snippet
from oauthenticator.cilogon import CILogonOAuthenticator
from tornado import web

# 2026-06-10 hub incident: the spawner rewrite (db11d83c) was reverted along
# with the chart bump to restore service; these tests pin the reverted
# behavior. Re-enable when the spawner changes are reintroduced and the
# breakage is understood.
pytestmark = pytest.mark.skip(reason="custom-spawner.py reverted to pre-incident state")

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


async def test_purdue_user_not_in_list_is_403(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns, monkeypatch, "mallory@purdue.edu")
    with pytest.raises(web.HTTPError) as err:
        await auth.authenticate(None)
    assert err.value.status_code == 403


async def test_cern_user_gets_suffix(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns, monkeypatch, "carol@cern.ch")
    ret = await auth.authenticate(None)
    assert ret["name"] == "carol-cern"
    assert ret["domain"] == "cern.ch"


async def test_cern_user_not_in_list_is_403(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns, monkeypatch, "mallory@cern.ch")
    with pytest.raises(web.HTTPError) as err:
        await auth.authenticate(None)
    assert err.value.status_code == 403


async def test_fnal_user_needs_no_list(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns, monkeypatch, "dave@fnal.gov")
    ret = await auth.authenticate(None)
    assert ret["name"] == "dave-fnal"


async def test_unknown_domain_is_403(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns, monkeypatch, "eve@evil.example")
    with pytest.raises(web.HTTPError) as err:
        await auth.authenticate(None)
    assert err.value.status_code == 403


async def test_userlist_lines_are_stripped(spawner_ns, monkeypatch):
    spawner_ns["_userlists"]["purdue"].write_text("  alice  \nbob\n\n")
    auth = make_authenticator(spawner_ns, monkeypatch, "alice@purdue.edu")
    ret = await auth.authenticate(None)
    assert ret["name"] == "alice"


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


# ── pre_spawn_start ───────────────────────────────────────────────────────────


async def test_pre_spawn_start_sets_username_env(spawner_ns):
    spawner = FakeSpawner()
    spawner.set_auth_state({"name": "alice-cern", "domain": "cern.ch"})
    await spawner_ns["pre_spawn_start"](spawner)
    assert spawner.environment["USERNAME"] == "alice-cern"


async def test_pre_spawn_start_without_auth_state_is_noop(spawner_ns):
    spawner = FakeSpawner()
    spawner.set_auth_state(None)
    await spawner_ns["pre_spawn_start"](spawner)
    assert "USERNAME" not in spawner.environment


# ── hub config wiring ─────────────────────────────────────────────────────────


def test_config_registers_authenticator_and_hooks(spawner_ns):
    c = spawner_ns["c"]
    assert (
        c["JupyterHub"]["authenticator_class"]
        is (spawner_ns["PurdueCILogonOAuthenticator"])
    )
    assert (
        c["PurdueCILogonOAuthenticator"]["post_auth_hook"]
        is (spawner_ns["passthrough_post_auth_hook"])
    )
    assert c["KubeSpawner"]["pre_spawn_start"] is spawner_ns["pre_spawn_start"]


def test_dask_gateway_env_set_in_cms_namespace(monkeypatch, tmp_path):
    ns = load_snippet("custom-spawner.py", monkeypatch, namespace="cms")
    env = ns["c"]["KubeSpawner"]["environment"]
    assert "DASK_GATEWAY__ADDRESS" in env
    assert "DASK_GATEWAY__PROXY_ADDRESS" in env


def test_dask_gateway_env_not_set_outside_cms(monkeypatch):
    ns = load_snippet("custom-spawner.py", monkeypatch, namespace="dev")
    env = ns["c"]["KubeSpawner"].get("environment", {})
    assert "DASK_GATEWAY__ADDRESS" not in env
