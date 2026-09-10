"""Tests for extraFiles/set-user-info.py — UID/GID mapping at spawn time."""

import pytest
from hub_helpers import FakeSpawner, load_snippet

ALL_PEOPLE = "ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"


def load(monkeypatch, fake_ldap, namespace="cms"):
    return load_snippet("set-user-info.py", monkeypatch, namespace=namespace)


# ── ldap_lookup ───────────────────────────────────────────────────────────────


def test_ldap_lookup_parses_uid_gid(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    uid, gid = ns["ldap_lookup"]("alice")
    assert (uid, gid) == (12345, 67890)


def test_ldap_lookup_targets_geddes_auth(monkeypatch, fake_ldap):
    """geddes-aux was retired; the lookup must hit geddes-auth under the
    AllPeople tree. Host and base DN move together — the old
    ou=People,dc=rcac base does not exist on the new server."""
    ns = load(monkeypatch, fake_ldap)
    ns["ldap_lookup"]("alice")
    assert fake_ldap["hosts"] == ["geddes-auth.rcac.purdue.edu"]
    assert fake_ldap["bases"] == [f"uid=alice,{ALL_PEOPLE}"]


def test_ldap_lookup_reads_the_dn_instead_of_searching(monkeypatch, fake_ldap):
    """Regression: geddes-auth has no uid index, so a filtered search under
    AllPeople scans the tree for 15-25s and blocks the Hub. A base-scope read
    of the account's own DN returns the same attributes immediately."""
    ns = load(monkeypatch, fake_ldap)
    ns["ldap_lookup"]("alice")
    assert fake_ldap["scopes"] == ["BASE"]
    assert fake_ldap["searches"] == ["(objectClass=*)"]


def test_ldap_lookup_falls_back_to_search_when_dn_is_empty(monkeypatch, fake_ldap):
    """An entry that is not at uid=<name>,<base> is still found, slowly."""
    fake_ldap["missing_dns"].add(f"uid=alice,{ALL_PEOPLE}")
    ns = load(monkeypatch, fake_ldap)

    assert ns["ldap_lookup"]("alice") == (12345, 67890)

    assert fake_ldap["scopes"] == ["BASE", "SUBTREE"]
    assert fake_ldap["searches"] == ["(objectClass=*)", "(uid=alice*)"]
    assert fake_ldap["bases"] == [f"uid=alice,{ALL_PEOPLE}", ALL_PEOPLE]


def test_ldap_lookup_raises_when_the_user_is_absent(monkeypatch, fake_ldap):
    fake_ldap["missing_dns"].update({f"uid=alice,{ALL_PEOPLE}", ALL_PEOPLE})
    ns = load(monkeypatch, fake_ldap)

    with pytest.raises(RuntimeError, match="no LDAP entry for alice"):
        ns["ldap_lookup"]("alice")


# ── passthrough_auth_state_hook ───────────────────────────────────────────────


async def test_purdue_user_resolved_via_ldap(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner()

    await ns["passthrough_auth_state_hook"](
        spawner, {"name": "alice", "domain": "purdue.edu"}
    )

    assert spawner.environment["NB_USER"] == "alice"
    assert spawner.environment["NB_UID"] == "12345"
    assert spawner.environment["NB_GID"] == "67890"
    assert fake_ldap["bases"] == [f"uid=alice,{ALL_PEOPLE}"]


async def test_external_user_mapped_to_paf_account(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner(user_id=7)

    await ns["passthrough_auth_state_hook"](
        spawner, {"name": "carol-cern", "domain": "cern.ch"}
    )

    # external users keep their hub username but get a mapped paf account uid
    assert spawner.environment["NB_USER"] == "carol-cern"
    assert fake_ldap["bases"] == [f"uid=paf0007,{ALL_PEOPLE}"]
    assert spawner.environment["NB_UID"] == "12345"


async def test_external_user_beyond_account_pool_refuses_spawn(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner(user_id=400)

    with pytest.raises(RuntimeError, match="ran out of accounts"):
        await ns["passthrough_auth_state_hook"](
            spawner, {"name": "dave-cern", "domain": "cern.ch"}
        )

    # no LDAP lookup for a nonexistent paf account, no UID/GID assigned
    assert fake_ldap["searches"] == []
    assert "NB_UID" not in spawner.environment
    assert "NB_GID" not in spawner.environment


async def test_lookup_does_not_block_the_event_loop(monkeypatch, fake_ldap):
    """ldap3 is synchronous: a slow directory must stall only the spawn that
    asked for it, not every other request the Hub is serving."""
    import asyncio
    import time

    ns = load(monkeypatch, fake_ldap)
    real_lookup = ns["ldap_lookup"]
    monkeypatch.setitem(
        ns, "ldap_lookup", lambda u: (time.sleep(0.3), real_lookup(u))[1]
    )

    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    await ns["passthrough_auth_state_hook"](
        FakeSpawner(), {"name": "alice", "domain": "purdue.edu"}
    )
    ticker.cancel()

    assert ticks > 5


async def test_pixi_home_points_to_work_storage(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner()

    await ns["passthrough_auth_state_hook"](
        spawner, {"name": "alice", "domain": "purdue.edu"}
    )

    assert spawner.environment["PIXI_HOME"] == "/work/users/alice/.pixi-home"


async def test_userdata_recorded_on_spawner(monkeypatch, fake_ldap):
    ns = load(monkeypatch, fake_ldap)
    spawner = FakeSpawner()

    await ns["passthrough_auth_state_hook"](
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
