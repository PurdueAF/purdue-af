"""Tests for extraFiles/cull-gpu-sessions.py — the 12h idle cull of full-GPU pods."""

import datetime
import types

from common import REPO, load_script

SCRIPT = (
    REPO / "apps" / "jupyterhub" / "jupyterhub" / "extraFiles" / "cull-gpu-sessions.py"
)
FULL = "nvidia.com/mig-7g.40gb"

NOW = datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.timezone.utc)
TWELVE_HOURS = 43200


def hours_ago(hours):
    """ISO timestamp `hours` before the real clock (cull_once uses real now)."""
    real_now = datetime.datetime.now(datetime.timezone.utc)
    return iso(real_now - datetime.timedelta(hours=hours))


def culler():
    return load_script(SCRIPT, "cull_gpu_sessions")


def fake_pod(limits=None, annotations=None, phase="Running"):
    container = types.SimpleNamespace(
        resources=types.SimpleNamespace(limits=limits) if limits is not None else None
    )
    return types.SimpleNamespace(
        spec=types.SimpleNamespace(containers=[container]),
        metadata=types.SimpleNamespace(annotations=annotations),
        status=types.SimpleNamespace(phase=phase),
    )


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ── pod inspection ────────────────────────────────────────────────────────────


def test_pod_holds_full_gpu():
    mod = culler()
    assert mod.pod_holds_full_gpu(fake_pod(limits={FULL: "1"}))
    assert not mod.pod_holds_full_gpu(fake_pod(limits={FULL: 0}))
    assert not mod.pod_holds_full_gpu(fake_pod(limits={"nvidia.com/mig-1g.5gb": "1"}))
    assert not mod.pod_holds_full_gpu(fake_pod(limits=None))  # sidecar w/o resources


def test_pod_server_reads_kubespawner_annotations():
    mod = culler()
    pod = fake_pod(
        annotations={
            "hub.jupyter.org/username": "alice-cern",
            "hub.jupyter.org/servername": "",
        }
    )
    assert mod.pod_server(pod) == ("alice-cern", "")
    # not hub-spawned (or annotations stripped) -> ignored
    assert mod.pod_server(fake_pod(annotations=None)) == (None, "")


# ── idleness ──────────────────────────────────────────────────────────────────


def test_idle_seconds():
    mod = culler()
    an_hour_ago = iso(NOW - datetime.timedelta(hours=1))
    assert mod.idle_seconds({"last_activity": an_hour_ago}, NOW) == 3600
    # no activity yet: fall back to spawn time
    assert mod.idle_seconds({"started": an_hour_ago}, NOW) == 3600
    assert mod.idle_seconds({}, NOW) == 0


# ── cull pass ─────────────────────────────────────────────────────────────────


def wire(mod, servers, users):
    """Stub the k8s and hub-API edges; record every hub REST call."""
    calls = []

    async def fake_full_gpu_servers(namespace):
        return servers

    async def fake_hub_api(method, path):
        calls.append((method, path))
        if method == "GET":
            return users[path.removeprefix("/users/")]
        return None

    mod.full_gpu_servers = fake_full_gpu_servers
    mod.hub_api = fake_hub_api
    return calls


async def test_culls_only_servers_idle_past_timeout():
    mod = culler()
    calls = wire(
        mod,
        servers=[("alice", ""), ("bob", "")],
        users={
            "alice": {"servers": {"": {"last_activity": hours_ago(1)}}},
            "bob": {"servers": {"": {"last_activity": hours_ago(13)}}},
        },
    )

    await mod.cull_once("cms", TWELVE_HOURS)

    assert ("DELETE", "/users/bob/server") in calls
    assert not any(m == "DELETE" and "alice" in p for m, p in calls)


async def test_skips_pending_and_vanished_servers():
    mod = culler()
    calls = wire(
        mod,
        servers=[("carol", ""), ("dave", "")],
        users={
            "carol": {
                "servers": {"": {"last_activity": hours_ago(48), "pending": "stop"}}
            },
            "dave": {"servers": {}},  # pod seen, but hub no longer tracks a server
        },
    )

    await mod.cull_once("cms", TWELVE_HOURS)

    assert not any(method == "DELETE" for method, _ in calls)


async def test_named_servers_and_special_usernames_are_quoted():
    mod = culler()
    calls = wire(
        mod,
        servers=[("eve@cern.ch", "gpu-box")],
        users={
            "eve%40cern.ch": {"servers": {"gpu-box": {"last_activity": hours_ago(13)}}}
        },
    )

    await mod.cull_once("cms", TWELVE_HOURS)

    assert ("DELETE", "/users/eve%40cern.ch/servers/gpu-box") in calls
