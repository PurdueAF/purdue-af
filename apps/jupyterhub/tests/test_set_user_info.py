"""Tests for extraFiles/set-user-info.py — UID/GID mapping at spawn time."""

from conftest import FakeSpawner, load_snippet


def load(monkeypatch, fake_ldap, namespace="cms"):
    return load_snippet("set-user-info.py", monkeypatch, namespace=namespace)


# ── ldap_lookup ───────────────────────────────────────────────────────────────


def test_ldap_lookup_parses_uid_gid(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    uid, gid = ns["ldap_lookup"]("alice")
    assert (uid, gid) == (12345, 67890)
    assert fake_ldap["searches"] == ["(uid=alice*)"]


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
    assert fake_ldap["searches"] == ["(uid=alice*)"]


def test_external_user_mapped_to_paf_account_in_cms(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap, namespace="cms")
    spawner = FakeSpawner(user_id=7)

    ns["passthrough_auth_state_hook"](
        spawner, {"name": "carol-cern", "domain": "cern.ch"}
    )

    # external users keep their hub username but get a mapped paf account uid
    assert spawner.environment["NB_USER"] == "carol-cern"
    assert fake_ldap["searches"] == ["(uid=paf0007*)"]
    assert spawner.environment["NB_UID"] == "12345"


def test_external_user_in_dev_namespace_gets_default_ids(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap, namespace="dev")
    spawner = FakeSpawner(user_id=7)

    ns["passthrough_auth_state_hook"](
        spawner, {"name": "carol-cern", "domain": "cern.ch"}
    )

    assert spawner.environment["NB_UID"] == "1000"
    assert spawner.environment["NB_GID"] == "1000"
    assert fake_ldap["searches"] == []  # no LDAP in dev


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
