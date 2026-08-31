"""Tests for extraFiles/set-user-info.py — UID/GID mapping at spawn time."""

import pytest
from hub_helpers import FakeSpawner, load_snippet


def load(monkeypatch, fake_ldap, namespace="cms"):
    return load_snippet("set-user-info.py", monkeypatch, namespace=namespace)


# ── ldap_lookup ───────────────────────────────────────────────────────────────


def test_ldap_lookup_parses_uid_gid(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    uid, gid = ns["ldap_lookup"]("alice")
    assert (uid, gid) == (12345, 67890)
    assert fake_ldap["searches"] == [
        "(&(objectClass=inetOrgPerson)(|(uid=alice)(cn=alice)))"
    ]


def test_ldap_lookup_no_entries_raises(monkeypatch, fake_ldap):
    fake_ldap["empty"] = True
    ns = load(monkeypatch, fake_ldap)
    with pytest.raises(RuntimeError, match="no entries"):
        ns["ldap_lookup"]("missing")


# ── passthrough_auth_state_hook ───────────────────────────────────────────────


def test_purdue_user_resolved_via_ldap(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner()

    ns["passthrough_auth_state_hook"](
        spawner, {"name": "alice", "domain": "purdue.edu"}
    )

    assert spawner.environment["NB_USER"] == "alice"
    assert spawner.environment["NB_UID"] == "12345"
    assert spawner.environment["NB_GID"] == "67890"
    assert fake_ldap["searches"] == [
        "(&(objectClass=inetOrgPerson)(|(uid=alice)(cn=alice)))"
    ]


def test_external_user_mapped_to_paf_account(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner(user_id=7)

    ns["passthrough_auth_state_hook"](
        spawner, {"name": "carol-cern", "domain": "cern.ch"}
    )

    # external users keep their hub username but get a mapped paf account uid
    assert spawner.environment["NB_USER"] == "carol-cern"
    assert fake_ldap["searches"] == [
        "(&(objectClass=inetOrgPerson)(|(uid=paf0007)(cn=paf0007)))"
    ]
    assert spawner.environment["NB_UID"] == "12345"


def test_external_user_beyond_account_pool_refuses_spawn(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner(user_id=400)

    with pytest.raises(RuntimeError, match="ran out of accounts"):
        ns["passthrough_auth_state_hook"](
            spawner, {"name": "dave-cern", "domain": "cern.ch"}
        )

    # no LDAP lookup for a nonexistent paf account, no UID/GID assigned
    assert fake_ldap["searches"] == []
    assert "NB_UID" not in spawner.environment
    assert "NB_GID" not in spawner.environment


def test_pixi_home_points_to_work_storage(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner()

    ns["passthrough_auth_state_hook"](
        spawner, {"name": "alice", "domain": "purdue.edu"}
    )

    assert spawner.environment["PIXI_HOME"] == "/work/users/alice/.pixi-home"


def test_userdata_recorded_on_spawner(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner()

    ns["passthrough_auth_state_hook"](
        spawner, {"name": "alice", "domain": "purdue.edu"}
    )

    assert spawner.userdata == {"name": "alice", "domain": "purdue.edu"}


# ── hub config wiring ─────────────────────────────────────────────────────────


def test_config_registers_hook_and_spawner_settings(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    c = ns["c"]
    assert c["KubeSpawner"]["auth_state_hook"] is ns["passthrough_auth_state_hook"]
    assert c["KubeSpawner"]["disable_user_config"] is True
    assert c["JupyterHub"]["authenticate_prometheus"] is False
