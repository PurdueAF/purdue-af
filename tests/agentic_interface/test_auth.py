"""Tests for auth.resolve_user: Hub API validation + token caching."""

import auth
import httpx
import respx

HUB_USER_URL = f"{auth.HUB_API_URL}/user"


def hub_user_payload(name="alice", pod_name="purdue-af-alice"):
    return {
        "name": name,
        "servers": {"": {"state": {"pod_name": pod_name}}},
    }


@respx.mock
async def test_valid_token_resolves_user():
    respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    user = await auth.resolve_user("tok-1")

    assert user == {
        "username": "alice",
        "pod_name": "purdue-af-alice",
        "namespace": auth.NAMESPACE,
        "token": "tok-1",
    }


@respx.mock
async def test_token_is_sent_as_bearer_header():
    route = respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    await auth.resolve_user("tok-1")

    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
async def test_invalid_token_returns_none():
    respx.get(HUB_USER_URL).respond(403)

    assert await auth.resolve_user("bad-token") is None


@respx.mock
async def test_hub_unreachable_returns_none():
    respx.get(HUB_USER_URL).mock(side_effect=httpx.ConnectError("boom"))

    assert await auth.resolve_user("tok-1") is None


@respx.mock
async def test_payload_without_username_returns_none():
    respx.get(HUB_USER_URL).respond(200, json={"servers": {}})

    assert await auth.resolve_user("tok-1") is None


@respx.mock
async def test_no_running_server_gives_empty_pod_name():
    respx.get(HUB_USER_URL).respond(200, json={"name": "alice", "servers": {}})

    user = await auth.resolve_user("tok-1")

    assert user["pod_name"] == ""


@respx.mock
async def test_second_call_is_served_from_cache():
    route = respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    first = await auth.resolve_user("tok-1")
    second = await auth.resolve_user("tok-1")

    assert first == second
    assert route.call_count == 1


@respx.mock
async def test_failed_validation_is_not_cached():
    route = respx.get(HUB_USER_URL).respond(403)

    assert await auth.resolve_user("tok-1") is None
    assert await auth.resolve_user("tok-1") is None
    assert route.call_count == 2


@respx.mock
async def test_cache_is_per_token():
    route = respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    await auth.resolve_user("tok-1")
    await auth.resolve_user("tok-2")

    assert route.call_count == 2


@respx.mock
async def test_clear_user_cache_forces_refetch():
    route = respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    await auth.resolve_user("tok-1")
    auth.clear_user_cache("tok-1")
    await auth.resolve_user("tok-1")

    assert route.call_count == 2


@respx.mock
async def test_cache_expires_after_ttl(monkeypatch):
    route = respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    now = 1000.0
    monkeypatch.setattr(auth.time, "monotonic", lambda: now)
    await auth.resolve_user("tok-1")

    now += auth._CACHE_TTL + 1
    await auth.resolve_user("tok-1")

    assert route.call_count == 2


@respx.mock
async def test_cache_is_bounded(monkeypatch):
    respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())
    monkeypatch.setattr(auth, "_CACHE_MAX", 3)

    for i in range(10):
        await auth.resolve_user(f"tok-{i}")

    assert len(auth._user_cache) <= 3


@respx.mock
async def test_eviction_prefers_expired_entries(monkeypatch):
    respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())
    monkeypatch.setattr(auth, "_CACHE_MAX", 2)

    now = 1000.0
    monkeypatch.setattr(auth.time, "monotonic", lambda: now)
    await auth.resolve_user("tok-old")

    now += auth._CACHE_TTL + 1  # tok-old is now expired
    await auth.resolve_user("tok-live")
    await auth.resolve_user("tok-new")  # at cap — must evict tok-old, not tok-live

    assert "tok-old" not in auth._user_cache
    assert "tok-live" in auth._user_cache
    assert "tok-new" in auth._user_cache


@respx.mock
async def test_hub_client_is_reused():
    respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    await auth.resolve_user("tok-1")
    client = auth._client
    await auth.resolve_user("tok-2")

    assert client is not None
    assert auth._client is client


@respx.mock
async def test_refetch_picks_up_new_pod_name():
    """After a session restart the pod name changes; cache clear must surface it."""
    route = respx.get(HUB_USER_URL)
    route.respond(200, json=hub_user_payload(pod_name="pod-old"))

    user = await auth.resolve_user("tok-1")
    assert user["pod_name"] == "pod-old"

    route.respond(200, json=hub_user_payload(pod_name="pod-new"))
    auth.clear_user_cache("tok-1")

    user = await auth.resolve_user("tok-1")
    assert user["pod_name"] == "pod-new"
