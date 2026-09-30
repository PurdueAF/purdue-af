"""Tests for extraFiles/set-user-info.py — UID/GID mapping at spawn time."""

import pytest
from hub_helpers import FakeSpawner, load_snippet

ALL_PEOPLE = "ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"


def load(monkeypatch):
    return load_snippet("set-user-info.py", monkeypatch)


# ── ldap_lookup ───────────────────────────────────────────────────────────────


def test_ldap_lookup_parses_uid_gid(monkeypatch, fake_ldap):
    ns = load(monkeypatch)
    uid, gid = ns["ldap_lookup"]("alice")
    assert (uid, gid) == (12345, 67890)


def test_ldap_lookup_targets_geddes_auth(monkeypatch, fake_ldap):
    """geddes-aux was retired; the lookup must hit geddes-auth under the
    AllPeople tree. Host and base DN move together — the old
    ou=People,dc=rcac base does not exist on the new server."""
    ns = load(monkeypatch)
    ns["ldap_lookup"]("alice")
    assert fake_ldap["hosts"] == ["geddes-auth.rcac.purdue.edu"]
    assert fake_ldap["bases"] == [f"uid=alice,{ALL_PEOPLE}"]
    assert fake_ldap["sessions"] == [(True, "start_tls")]


def test_ldap_lookup_binds_in_plaintext_for_the_e2e_mock(monkeypatch, fake_ldap):
    monkeypatch.setenv("AF_LDAP_HOST", "ldap-mock")
    monkeypatch.setenv("AF_LDAP_TLS", "false")
    ns = load(monkeypatch)

    assert ns["ldap_lookup"]("alice") == (12345, 67890)
    assert fake_ldap["hosts"] == ["ldap-mock"]
    assert fake_ldap["sessions"] == [(False, "bind")]


def test_ldap_lookup_reads_the_dn_instead_of_searching(monkeypatch, fake_ldap):
    """A base-scope read of the account's DN, never a filtered search: the
    directory has no uid index, and a search blocks the Hub's event loop."""
    ns = load(monkeypatch)
    ns["ldap_lookup"]("alice")
    assert fake_ldap["scopes"] == ["BASE"]
    assert fake_ldap["searches"] == ["(objectClass=*)"]


def test_ldap_lookup_falls_back_to_search_when_dn_is_empty(monkeypatch, fake_ldap):
    """An entry that is not at uid=<name>,<base> is still found, slowly."""
    fake_ldap["missing_dns"].add(f"uid=alice,{ALL_PEOPLE}")
    ns = load(monkeypatch)

    assert ns["ldap_lookup"]("alice") == (12345, 67890)

    assert fake_ldap["scopes"] == ["BASE", "SUBTREE"]
    assert fake_ldap["searches"] == ["(objectClass=*)", "(uid=alice*)"]
    assert fake_ldap["bases"] == [f"uid=alice,{ALL_PEOPLE}", ALL_PEOPLE]


def test_ldap_lookup_raises_when_the_user_is_absent(monkeypatch, fake_ldap):
    fake_ldap["missing_dns"].update({f"uid=alice,{ALL_PEOPLE}", ALL_PEOPLE})
    ns = load(monkeypatch)

    with pytest.raises(RuntimeError, match="no LDAP entry for alice"):
        ns["ldap_lookup"]("alice")


# ── passthrough_auth_state_hook ───────────────────────────────────────────────


async def test_purdue_user_resolved_via_ldap(monkeypatch, fake_ldap):
    ns = load(monkeypatch)
    spawner = FakeSpawner()

    await ns["passthrough_auth_state_hook"](
        spawner, {"name": "alice", "domain": "purdue.edu"}
    )

    assert spawner.environment["NB_USER"] == "alice"
    assert spawner.environment["NB_UID"] == "12345"
    assert spawner.environment["NB_GID"] == "67890"
    assert fake_ldap["bases"] == [f"uid=alice,{ALL_PEOPLE}"]


# ── pooled accounts ───────────────────────────────────────────────────────────


async def spawn_external(ns, spawner, name="carol-cern"):
    await ns["passthrough_auth_state_hook"](
        spawner, {"name": name, "domain": "cern.ch"}
    )


async def test_purdue_user_never_touches_the_ledger(monkeypatch, fake_ldap):
    ns = load(monkeypatch)
    spawner = FakeSpawner()

    await ns["passthrough_auth_state_hook"](
        spawner, {"name": "alice", "domain": "purdue.edu"}
    )

    assert spawner.api.calls == []


async def test_external_user_keeps_the_account_the_ledger_records(
    monkeypatch, fake_ldap
):
    """The hub id plays no part: the entry is what the user runs as."""
    ns = load(monkeypatch)
    spawner = FakeSpawner(user_id=7, ledger={"carol-cern": "paf0042"})

    await spawn_external(ns, spawner)

    assert spawner.environment["NB_USER"] == "carol-cern"
    assert fake_ldap["bases"] == [f"uid=paf0042,{ALL_PEOPLE}"]
    assert spawner.environment["NB_UID"] == "12345"
    assert [call[0] for call in spawner.api.calls] == ["read"]
    assert spawner.api.data == {"carol-cern": "paf0042"}


async def test_a_new_external_user_gets_the_account_of_their_hub_id(
    monkeypatch, fake_ldap
):
    ns = load(monkeypatch)
    ledger = {"ann-cern": "paf0000", "deleted-1": "paf0001"}
    spawner = FakeSpawner(user_id=250, ledger=ledger)

    await spawn_external(ns, spawner)

    assert fake_ldap["bases"] == [f"uid=paf0250,{ALL_PEOPLE}"]
    assert spawner.api.data == {**ledger, "carol-cern": "paf0250"}
    assert [call[0] for call in spawner.api.calls] == ["read", "replace"]

    # the entry outlives the spawn: the next one reads it back
    again = FakeSpawner(user_id=250, api=spawner.api)
    await spawn_external(ns, again)
    assert fake_ldap["bases"][-1] == f"uid=paf0250,{ALL_PEOPLE}"
    assert spawner.api.data == {**ledger, "carol-cern": "paf0250"}


async def test_a_held_hub_id_account_falls_back_to_the_highest_free(
    monkeypatch, fake_ldap
):
    """A reused hub id never inherits: the deleted user's entry still holds it.
    The fallback starts at the top of the pool, which no hub id has reached."""
    ns = load(monkeypatch)
    ledger = {
        "ann-cern": "paf0000",
        "deleted-1": "paf0001",
        "bob-fnal": "paf0003",
        "gone-cern": "paf0250",
    }
    spawner = FakeSpawner(user_id=250, ledger=ledger)

    await spawn_external(ns, spawner)

    assert fake_ldap["bases"] == [f"uid=paf0399,{ALL_PEOPLE}"]
    assert spawner.api.data == {**ledger, "carol-cern": "paf0399"}


async def test_a_hub_id_beyond_the_pool_gets_the_highest_free(monkeypatch, fake_ldap):
    ns = load(monkeypatch)
    spawner = FakeSpawner(user_id=400, ledger={"ann-cern": "paf0399"})

    await spawn_external(ns, spawner)

    assert fake_ldap["bases"] == [f"uid=paf0398,{ALL_PEOPLE}"]


async def test_a_missing_ledger_is_seeded_from_the_hubs_users(monkeypatch, fake_ldap):
    """Every external user keeps the account of their hub id; a hub id with
    no user reserves its account, which may still own files."""
    ns = load(monkeypatch)
    users = [
        (1, "alice"),
        (2, "bob-cern"),
        (3, "carol-fnal"),
        (5, "dave"),
        (6, "eve-cern"),
    ]
    spawner = FakeSpawner(user_id=6, users=users)

    await spawn_external(ns, spawner, "eve-cern")

    assert fake_ldap["bases"] == [f"uid=paf0006,{ALL_PEOPLE}"]
    assert spawner.api.data == {
        "bob-cern": "paf0002",
        "carol-fnal": "paf0003",
        "deleted-4": "paf0004",
        "eve-cern": "paf0006",
    }
    assert [call[0] for call in spawner.api.calls] == ["read", "create"]


def test_seed_ledger_leaves_out_a_name_that_is_no_configmap_key(monkeypatch, fake_ldap):
    """One such row must not fail the seed, and with it every external spawn."""
    ns = load(monkeypatch)

    seed = ns["seed_ledger"](
        [(1, "alice"), (2, "bob-cern"), (3, "bad+name-cern"), (4, "eve-fnal")]
    )

    assert seed == {"bob-cern": "paf0002", "eve-fnal": "paf0004"}


async def test_a_name_that_is_no_configmap_key_fails_its_own_spawn_only(
    monkeypatch, fake_ldap
):
    ns = load(monkeypatch)
    spawner = FakeSpawner(user_id=3, ledger={"bob-cern": "paf0002"})

    with pytest.raises(RuntimeError, match="cannot be a key"):
        await spawn_external(ns, spawner, "bad+name-cern")

    assert spawner.api.calls == []
    assert "NB_UID" not in spawner.environment


def test_seed_ledger_skips_hub_ids_beyond_the_pool(monkeypatch, fake_ldap):
    ns = load(monkeypatch)
    users = [(1, "alice"), (2, "bob-cern"), (400, "eve-cern"), (401, "fay-fnal")]

    seed = ns["seed_ledger"](users)

    assert seed["bob-cern"] == "paf0002"
    assert "eve-cern" not in seed and "fay-fnal" not in seed
    reserved = {key for key in seed if key.startswith("deleted-")}
    assert reserved == {f"deleted-{n}" for n in range(3, 400)}
    assert ns["seed_ledger"]([]) == {}
    # the reservation runs up to the highest hub id of any user
    assert ns["seed_ledger"]([(1, "a-cern"), (3, "p")]) == {
        "a-cern": "paf0001",
        "deleted-2": "paf0002",
    }


async def test_a_user_outside_the_seed_is_allocated_after_it(monkeypatch, fake_ldap):
    ns = load(monkeypatch)
    users = [(1, "alice"), (2, "bob-cern"), (3, "carol-cern")]
    spawner = FakeSpawner(user_id=3, users=users)
    # another hub created the ledger first, without carol
    spawner.api.race_create = {"bob-cern": "paf0002"}

    await spawn_external(ns, spawner)

    assert spawner.api.data == {"bob-cern": "paf0002", "carol-cern": "paf0003"}
    assert [call[0] for call in spawner.api.calls] == [
        "read",
        "create",
        "read",
        "replace",
    ]


async def test_a_concurrent_allocation_is_redone_against_the_new_ledger(
    monkeypatch, fake_ldap
):
    ns = load(monkeypatch)
    spawner = FakeSpawner(user_id=1, ledger={"ann-cern": "paf0000"})
    spawner.api.on_conflict = lambda data: data.update({"bob-cern": "paf0001"})

    await spawn_external(ns, spawner)

    assert spawner.api.data == {
        "ann-cern": "paf0000",
        "bob-cern": "paf0001",
        "carol-cern": "paf0399",
    }
    assert [call[0] for call in spawner.api.calls] == [
        "read",
        "replace",
        "read",
        "replace",
    ]


async def test_a_ledger_that_keeps_changing_fails_the_spawn(monkeypatch, fake_ldap):
    ns = load(monkeypatch)
    spawner = FakeSpawner(ledger={})
    spawner.api.always_conflict = True

    with pytest.raises(RuntimeError, match="kept changing"):
        await spawn_external(ns, spawner)

    assert fake_ldap["searches"] == []
    assert "NB_UID" not in spawner.environment


async def test_an_exhausted_pool_refuses_the_spawn(monkeypatch, fake_ldap):
    ns = load(monkeypatch)
    full = {f"user{n}-cern": f"paf{n:04d}" for n in range(400)}
    spawner = FakeSpawner(ledger=full)

    with pytest.raises(RuntimeError, match="ran out of pooled accounts"):
        await spawn_external(ns, spawner)

    # no LDAP lookup for a nonexistent account, no UID/GID assigned
    assert fake_ldap["searches"] == []
    assert "NB_UID" not in spawner.environment
    assert "NB_GID" not in spawner.environment
    assert spawner.api.data == full


async def test_a_ledger_the_hub_cannot_read_fails_the_spawn(monkeypatch, fake_ldap):
    from kubernetes_asyncio.client.rest import ApiException

    ns = load(monkeypatch)
    spawner = FakeSpawner(ledger={})
    spawner.api.fail_read = 403

    with pytest.raises(ApiException):
        await spawn_external(ns, spawner)

    assert "NB_UID" not in spawner.environment


async def test_every_ledger_call_is_bounded_like_kubespawners_own(
    monkeypatch, fake_ldap
):
    """The hook runs before start_timeout is armed: an unbounded call would
    leave the spawn pending for good."""
    ns = load(monkeypatch)
    spawner = FakeSpawner(user_id=2, users=[(1, "alice"), (2, "carol-cern")])
    spawner.api.race_create = {}

    await spawn_external(ns, spawner)

    assert len(spawner.api.timeouts) == 4
    assert set(spawner.api.timeouts) == {spawner.k8s_api_request_timeout}


async def test_lookup_does_not_block_the_event_loop(monkeypatch, fake_ldap):
    """ldap3 is synchronous: a slow directory must stall only the spawn that
    asked for it, not every other request the Hub is serving."""
    import asyncio
    import time

    ns = load(monkeypatch)
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
    ns = load(monkeypatch)
    spawner = FakeSpawner()

    await ns["passthrough_auth_state_hook"](
        spawner, {"name": "alice", "domain": "purdue.edu"}
    )

    assert spawner.environment["PIXI_HOME"] == "/work/users/alice/.pixi-home"


async def test_userdata_recorded_on_spawner(monkeypatch, fake_ldap):
    ns = load(monkeypatch)
    spawner = FakeSpawner()

    await ns["passthrough_auth_state_hook"](
        spawner, {"name": "alice", "domain": "purdue.edu"}
    )

    assert spawner.userdata == {"name": "alice", "domain": "purdue.edu"}


# ── hub config wiring ─────────────────────────────────────────────────────────


def test_config_registers_hook_and_spawner_settings(monkeypatch, fake_ldap):
    ns = load(monkeypatch)
    c = ns["c"]
    assert c["KubeSpawner"]["auth_state_hook"] is ns["passthrough_auth_state_hook"]
    assert c["KubeSpawner"]["disable_user_config"] is True
