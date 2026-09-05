"""Tests for extraFiles/set-user-info.py — UID/GID mapping at spawn time."""

import asyncio

import pytest
from hub_helpers import FakeSpawner, load_snippet


def load(monkeypatch, fake_ldap, namespace="cms"):
    return load_snippet("set-user-info.py", monkeypatch, namespace=namespace)


def run_hook(ns, spawner, auth_state):
    """The hook is a coroutine (the LDAP call runs in an executor)."""
    return asyncio.run(ns["passthrough_auth_state_hook"](spawner, auth_state))


# ── ldap_lookup ───────────────────────────────────────────────────────────────


def test_ldap_lookup_parses_uid_gid(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    uid, gid = ns["ldap_lookup"]("alice")
    assert (uid, gid) == (12345, 67890)
    assert fake_ldap["searches"] == ["(uid=alice*)"]


def test_ldap_lookup_targets_geddes_auth(monkeypatch, fake_ldap):
    """geddes-aux was retired; the lookup must hit geddes-auth under the
    AllPeople tree. Host and base DN move together — the old
    ou=People,dc=rcac base does not exist on the new server."""
    ns = load(monkeypatch, fake_ldap)
    ns["ldap_lookup"]("alice")
    assert fake_ldap["hosts"] == ["geddes-auth.rcac.purdue.edu"]
    assert fake_ldap["bases"] == ["ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"]


def test_ldap_lookup_binds_anonymously(monkeypatch, fake_ldap):
    """geddes-auth grants anonymous read to the 172.21.160.0/24 node subnet
    (and nothing else); there is no bind identity to configure."""
    ns = load(monkeypatch, fake_ldap)
    ns["ldap_lookup"]("alice")
    assert fake_ldap["binds"] == ["ANONYMOUS"]


def test_ldap_lookup_reports_bind_failure(monkeypatch, fake_ldap):
    monkeypatch.setenv(
        "AF_LDAP_BIND_DN", "uid=svc,ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"
    )
    monkeypatch.setenv("AF_LDAP_BIND_PASSWORD", "wrong")
    fake_ldap["bind_ok"] = False
    ns = load(monkeypatch, fake_ldap)
    with pytest.raises(
        RuntimeError, match="bind to geddes-auth.*49 invalidCredentials"
    ):
        ns["ldap_lookup"]("alice")
    assert fake_ldap["searches"] == []


def test_ldap_lookup_reports_search_error_not_missing_user(monkeypatch, fake_ldap):
    """What broke #203: an ACL denial returns 32 noSuchObject with zero
    entries, which the old code reported as an IndexError. The result code
    has to reach the log."""
    fake_ldap["result"] = {"result": 32, "description": "noSuchObject", "message": ""}
    fake_ldap["entries"] = []
    ns = load(monkeypatch, fake_ldap)
    with pytest.raises(RuntimeError, match="uid=alice on geddes-auth.*32 noSuchObject"):
        ns["ldap_lookup"]("alice")


def test_ldap_lookup_reports_missing_user(monkeypatch, fake_ldap):
    fake_ldap["entries"] = []
    ns = load(monkeypatch, fake_ldap)
    with pytest.raises(LookupError, match="no LDAP entry for uid=alice"):
        ns["ldap_lookup"]("alice")


# ── passthrough_auth_state_hook ───────────────────────────────────────────────


def test_purdue_user_resolved_via_ldap(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner()

    run_hook(ns, spawner, {"name": "alice", "domain": "purdue.edu"})

    assert spawner.environment["NB_USER"] == "alice"
    assert spawner.environment["NB_UID"] == "12345"
    assert spawner.environment["NB_GID"] == "67890"
    assert fake_ldap["searches"] == ["(uid=alice*)"]


def test_external_user_mapped_to_paf_account(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner(user_id=7)

    run_hook(ns, spawner, {"name": "carol-cern", "domain": "cern.ch"})

    # external users keep their hub username but get a mapped paf account uid
    assert spawner.environment["NB_USER"] == "carol-cern"
    assert fake_ldap["searches"] == ["(uid=paf0007*)"]
    assert spawner.environment["NB_UID"] == "12345"


def test_external_user_beyond_account_pool_refuses_spawn(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner(user_id=400)

    with pytest.raises(RuntimeError, match="ran out of accounts"):
        run_hook(ns, spawner, {"name": "dave-cern", "domain": "cern.ch"})

    # no LDAP lookup for a nonexistent paf account, no UID/GID assigned
    assert fake_ldap["searches"] == []
    assert "NB_UID" not in spawner.environment
    assert "NB_GID" not in spawner.environment


def test_pixi_home_points_to_work_storage(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner()

    run_hook(ns, spawner, {"name": "alice", "domain": "purdue.edu"})

    assert spawner.environment["PIXI_HOME"] == "/work/users/alice/.pixi-home"


def test_userdata_recorded_on_spawner(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner()

    run_hook(ns, spawner, {"name": "alice", "domain": "purdue.edu"})

    assert spawner.userdata == {"name": "alice", "domain": "purdue.edu"}


# ── hub config wiring ─────────────────────────────────────────────────────────


def test_config_registers_hook_and_spawner_settings(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    c = ns["c"]
    assert c["KubeSpawner"]["auth_state_hook"] is ns["passthrough_auth_state_hook"]
    assert c["KubeSpawner"]["disable_user_config"] is True
    assert c["JupyterHub"]["authenticate_prometheus"] is False
