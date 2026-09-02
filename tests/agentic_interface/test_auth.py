"""Tests for auth.resolve_user: Hub API validation + token caching."""

import auth
import httpx
import pytest
import respx
from cachetools import TTLCache
from prometheus_client import REGISTRY

HUB_USER_URL = f"{auth.HUB_API_URL}/user"


def _auth_counter_value(result: str) -> float:
    return (
        REGISTRY.get_sample_value("purdue_af_mcp_auth_total", {"result": result}) or 0.0
    )


def hub_user_payload(name="alice"):
    return {"name": name, "servers": {}}


@respx.mock
async def test_valid_token_resolves_user():
    respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    user = await auth.resolve_user("tok-1")

    assert user == {
        "username": "alice",
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
async def test_hub_unreachable_raises_not_invalid():
    """A Hub that cannot be asked says nothing about the token."""
    respx.get(HUB_USER_URL).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(auth.HubUnavailable, match="unreachable"):
        await auth.resolve_user("tok-1")


@respx.mock
async def test_payload_without_username_is_a_hub_fault():
    respx.get(HUB_USER_URL).respond(200, json={"servers": {}})

    with pytest.raises(auth.HubUnavailable, match="no user record"):
        await auth.resolve_user("bad-token")
    assert not auth._negative_cache  # the token was never judged


@respx.mock
async def test_second_call_is_served_from_cache():
    route = respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    first = await auth.resolve_user("tok-1")
    second = await auth.resolve_user("tok-1")

    assert first == second
    assert route.call_count == 1


@respx.mock
async def test_invalid_token_is_negatively_cached():
    route = respx.get(HUB_USER_URL).respond(403)

    assert await auth.resolve_user("tok-1") is None
    assert await auth.resolve_user("tok-1") is None  # served from negative cache

    assert route.call_count == 1


@respx.mock
async def test_negative_cache_expires_after_ttl(monkeypatch):
    route = respx.get(HUB_USER_URL).respond(403)

    now = 1000.0
    monkeypatch.setattr(auth.time, "monotonic", lambda: now)
    await auth.resolve_user("tok-1")

    now += auth._NEG_CACHE_TTL + 1
    await auth.resolve_user("tok-1")

    assert route.call_count == 2


@respx.mock
async def test_hub_unreachable_is_not_negatively_cached():
    route = respx.get(HUB_USER_URL).mock(side_effect=httpx.ConnectError("boom"))

    for _ in range(2):
        with pytest.raises(auth.HubUnavailable):
            await auth.resolve_user("tok-1")

    assert route.call_count == 2  # each request retried against the Hub
    assert not auth._negative_cache


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


def _small_cache(monkeypatch, maxsize):
    """Swap in a user cache with a smaller bound (the real one is built at import)."""
    cache = TTLCache(maxsize, auth._CACHE_TTL, timer=auth._now)
    monkeypatch.setattr(auth, "_user_cache", cache)
    return cache


@respx.mock
async def test_cache_is_bounded(monkeypatch):
    respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())
    _small_cache(monkeypatch, 3)

    for i in range(10):
        await auth.resolve_user(f"tok-{i}")

    assert len(auth._user_cache) <= 3


@respx.mock
async def test_eviction_prefers_expired_entries(monkeypatch):
    respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())
    _small_cache(monkeypatch, 2)

    now = 1000.0
    monkeypatch.setattr(auth.time, "monotonic", lambda: now)
    await auth.resolve_user("tok-old")

    now += auth._CACHE_TTL + 1  # tok-old is now expired
    await auth.resolve_user("tok-live")
    await auth.resolve_user("tok-new")  # at cap — must evict tok-old, not tok-live

    assert auth._hash_token("tok-old") not in auth._user_cache
    assert auth._hash_token("tok-live") in auth._user_cache
    assert auth._hash_token("tok-new") in auth._user_cache


@respx.mock
async def test_cache_keys_are_hashed_not_raw_tokens():
    respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    user = await auth.resolve_user("tok-secret")

    assert "tok-secret" not in auth._user_cache
    assert auth._hash_token("tok-secret") in auth._user_cache
    # the user_info dict still carries the raw token — tools need it downstream
    assert user["token"] == "tok-secret"


@respx.mock
async def test_negative_cache_keys_are_hashed_not_raw_tokens():
    respx.get(HUB_USER_URL).respond(403)

    await auth.resolve_user("tok-bad")

    assert "tok-bad" not in auth._negative_cache
    assert auth._hash_token("tok-bad") in auth._negative_cache


@respx.mock
async def test_hub_client_is_reused():
    respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    await auth.resolve_user("tok-1")
    client = auth._client
    await auth.resolve_user("tok-2")

    assert client is not None
    assert auth._client is client


@respx.mock
async def test_auth_metrics_record_each_result():
    route = respx.get(HUB_USER_URL)

    route.respond(200, json=hub_user_payload())
    before = {
        r: _auth_counter_value(r)
        for r in (
            "validated",
            "cache_hit",
            "neg_cache_hit",
            "invalid_token",
            "hub_unreachable",
        )
    }

    await auth.resolve_user("tok-1")  # validated
    await auth.resolve_user("tok-1")  # cache_hit

    route.respond(403)
    await auth.resolve_user("tok-bad")  # invalid_token
    await auth.resolve_user("tok-bad")  # neg_cache_hit

    route.mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(auth.HubUnavailable):
        await auth.resolve_user("tok-down")  # hub_unreachable

    assert _auth_counter_value("validated") == before["validated"] + 1
    assert _auth_counter_value("cache_hit") == before["cache_hit"] + 1
    assert _auth_counter_value("neg_cache_hit") == before["neg_cache_hit"] + 1
    assert _auth_counter_value("invalid_token") == before["invalid_token"] + 1
    assert _auth_counter_value("hub_unreachable") == before["hub_unreachable"] + 1


@respx.mock
async def test_hub_5xx_is_unavailable_not_invalid_and_not_cached():
    respx.get(HUB_USER_URL).respond(503, text="hub restarting")
    before = _auth_counter_value("hub_error")

    with pytest.raises(auth.HubUnavailable, match="returned HTTP 503"):
        await auth.resolve_user("tok-1")

    assert not auth._negative_cache
    assert _auth_counter_value("hub_error") == before + 1


@respx.mock
async def test_hub_404_names_the_misconfiguration():
    respx.get(HUB_USER_URL).respond(404)

    with pytest.raises(auth.HubUnavailable, match="JUPYTERHUB_API_URL"):
        await auth.resolve_user("tok-1")


@respx.mock
async def test_hub_401_is_invalid_token():
    respx.get(HUB_USER_URL).respond(401)

    assert await auth.resolve_user("tok-1") is None
    assert auth._negative_cache


# ── HubTokenVerifier (the MCP SDK TokenVerifier protocol) ─────────────────────


@respx.mock
async def test_verifier_maps_hub_user_to_access_token():
    respx.get(HUB_USER_URL).respond(200, json=hub_user_payload())

    access = await auth.HubTokenVerifier().verify_token("tok-1")

    assert access is not None
    assert access.client_id == "alice"
    assert access.token == "tok-1"


@respx.mock
async def test_verifier_returns_none_for_rejected_token_and_raises_for_hub_outage():
    respx.get(HUB_USER_URL).respond(403)
    assert await auth.HubTokenVerifier().verify_token("bad") is None

    respx.get(HUB_USER_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(auth.HubUnavailable):
        await auth.HubTokenVerifier().verify_token("tok-2")
