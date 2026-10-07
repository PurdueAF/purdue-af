"""Tests for extraFiles/custom-spawner.py — CILogon auth gating and hooks."""

import pytest
import yaml
from common import REPO
from hub_helpers import load_snippet
from tornado import web
from traitlets.config import Config

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


PURDUE_IDP = "https://idp.purdue.edu/idp/shibboleth"
CERN_IDP = "https://cern.ch/login"
FNAL_IDP = "https://idp.fnal.gov/idp/shibboleth"
VALUES = REPO / "apps" / "jupyterhub" / "jupyterhub" / "values.yaml"


def make_authenticator(ns):
    """The authenticator with the idps of the production values."""
    hub_config = yaml.safe_load(VALUES.read_text())["hub"]["config"]
    config = Config()
    config.PurdueCILogonOAuthenticator.idps = hub_config["PurdueCILogonOAuthenticator"][
        "idps"
    ]
    config.PurdueCILogonOAuthenticator.client_id = "id"
    config.PurdueCILogonOAuthenticator.client_secret = "secret"
    config.PurdueCILogonOAuthenticator.enable_auth_state = True
    return ns["PurdueCILogonOAuthenticator"](config=config)


def mock_cilogon(auth, monkeypatch, eppn, idp=PURDUE_IDP):
    """Stand in for the token and userinfo requests of the OAuth flow."""

    async def get_token_info(handler, params):
        return {"access_token": "token"}

    async def token_to_user(token_info):
        return {"eppn": eppn, "idp": idp}

    monkeypatch.setattr(auth, "build_access_tokens_request_params", lambda h, d: {})
    monkeypatch.setattr(auth, "get_token_info", get_token_info)
    monkeypatch.setattr(auth, "token_to_user", token_to_user)


# ── username mapping + userlist gates ─────────────────────────────────────────


def test_purdue_user_in_list(spawner_ns):
    auth = make_authenticator(spawner_ns)
    name = auth.user_info_to_username({"eppn": "alice@purdue.edu", "idp": PURDUE_IDP})
    assert name == "alice"


def test_purdue_user_not_in_list_is_denied(spawner_ns):
    auth = make_authenticator(spawner_ns)
    with pytest.raises(web.HTTPError) as err:
        auth.user_info_to_username({"eppn": "mallory@purdue.edu", "idp": PURDUE_IDP})
    assert err.value.status_code == 403
    assert "mallory" in str(err.value)


def test_cern_user_gets_suffix(spawner_ns):
    auth = make_authenticator(spawner_ns)
    name = auth.user_info_to_username({"eppn": "carol@cern.ch", "idp": CERN_IDP})
    assert name == "carol-cern"


def test_cern_user_not_in_list_is_denied(spawner_ns):
    auth = make_authenticator(spawner_ns)
    with pytest.raises(web.HTTPError) as err:
        auth.user_info_to_username({"eppn": "mallory@cern.ch", "idp": CERN_IDP})
    assert err.value.status_code == 403


def test_fnal_user_needs_no_list(spawner_ns):
    auth = make_authenticator(spawner_ns)
    name = auth.user_info_to_username({"eppn": "dave@fnal.gov", "idp": FNAL_IDP})
    assert name == "dave-fnal"


@pytest.mark.parametrize("eppn", ["eve@evil.example", "no-domain"])
def test_unknown_domain_is_denied(spawner_ns, eppn):
    auth = make_authenticator(spawner_ns)
    with pytest.raises(web.HTTPError) as err:
        auth.user_info_to_username({"eppn": eppn, "idp": PURDUE_IDP})
    assert err.value.status_code == 403


def test_unconfigured_idp_is_denied(spawner_ns):
    auth = make_authenticator(spawner_ns)
    with pytest.raises(web.HTTPError) as err:
        auth.user_info_to_username(
            {"eppn": "alice@purdue.edu", "idp": "https://idp.example/shibboleth"}
        )
    assert err.value.status_code == 403


def test_userlist_requires_exact_newline_terminated_line(spawner_ns):
    # Matching is `f"{username}\\n" in readlines()` — no strip.
    spawner_ns["_userlists"]["purdue"].write_text("  alice  \nbob\n")
    auth = make_authenticator(spawner_ns)
    with pytest.raises(web.HTTPError) as err:
        auth.user_info_to_username({"eppn": "alice@purdue.edu", "idp": PURDUE_IDP})
    assert err.value.status_code == 403


# ── the whole login, through JupyterHub's get_authenticated_user ──────────────


async def test_login_stores_identity_in_auth_state(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns)
    mock_cilogon(auth, monkeypatch, "carol@cern.ch", idp=CERN_IDP)
    model = await auth.get_authenticated_user(None, None)
    assert model["name"] == "carol-cern"
    assert model["auth_state"]["name"] == "carol-cern"
    assert model["auth_state"]["domain"] == "cern.ch"
    assert model["auth_state"]["cilogon_user"]["eppn"] == "carol@cern.ch"


async def test_login_normalizes_the_stored_name(spawner_ns, monkeypatch):
    spawner_ns["_userlists"]["purdue"].write_text("Alice\n")
    auth = make_authenticator(spawner_ns)
    mock_cilogon(auth, monkeypatch, "Alice@purdue.edu")
    model = await auth.get_authenticated_user(None, None)
    assert model["name"] == "alice"
    assert model["auth_state"]["name"] == "alice"


async def test_login_of_unlisted_user_is_denied(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns)
    mock_cilogon(auth, monkeypatch, "mallory@purdue.edu")
    with pytest.raises(web.HTTPError) as err:
        await auth.get_authenticated_user(None, None)
    assert err.value.status_code == 403


async def test_allow_all_does_not_open_the_userlist_gate(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns)
    auth.allow_all = True
    mock_cilogon(auth, monkeypatch, "mallory@purdue.edu")
    with pytest.raises(web.HTTPError) as err:
        await auth.get_authenticated_user(None, None)
    assert err.value.status_code == 403


async def test_admin_user_is_still_gated_by_the_userlist(spawner_ns, monkeypatch):
    auth = make_authenticator(spawner_ns)
    auth.admin_users = {"mallory"}
    mock_cilogon(auth, monkeypatch, "mallory@purdue.edu")
    with pytest.raises(web.HTTPError) as err:
        await auth.get_authenticated_user(None, None)
    assert err.value.status_code == 403


# ── hub config wiring ─────────────────────────────────────────────────────────


def test_config_registers_authenticator(spawner_ns):
    c = spawner_ns["c"]
    assert (
        c["JupyterHub"]["authenticator_class"]
        is spawner_ns["PurdueCILogonOAuthenticator"]
    )


def test_dask_gateway_env_set_in_cms_namespace(monkeypatch):
    ns = load_snippet("custom-spawner.py", monkeypatch, namespace="cms")
    env = ns["c"]["KubeSpawner"]["environment"]
    assert "DASK_GATEWAY__ADDRESS" in env
    assert "DASK_GATEWAY__PROXY_ADDRESS" in env
    assert env["DASK_GATEWAY__AUTH__TYPE"] == "jupyterhub"
