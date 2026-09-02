"""Token validation and user-context resolution.

Extracted into its own module so that tool modules (e.g. session.py) can
call clear_user_cache() after session state changes — without creating a
circular import with server.py.
"""

import hashlib
import os
import time
from typing import Optional

import httpx
from errors import describe_exception, json_body, response_detail
from metrics import instrumented_transport, record_auth

HUB_API_URL = os.environ.get("JUPYTERHUB_API_URL", "http://hub:8081/hub/api")
NAMESPACE = os.environ.get("NAMESPACE", "cms")


class HubUnavailable(Exception):
    """The Hub could not be asked whether a token is valid.

    Kept apart from an invalid token on purpose: the caller must answer
    "try again" (503), never "your token is wrong" (401), and nothing about
    the token may be cached either way.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# Cache keys are sha256 hex digests of the token, never the raw token — a
# cache dump (debugger, core, log) must not hand out live credentials. The
# cached user_info dict still carries the raw token; tools need it downstream.
# sha256(token) → (expiry_monotonic, user_info_dict)
_user_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 60.0
_CACHE_MAX = 1024

# Negative cache: tokens the Hub actively rejected. Short TTL so a garbage-
# token flood doesn't amplify into a live Hub API call per request, while a
# freshly issued token is never locked out for long. hub_unreachable errors
# are deliberately NOT cached — they say nothing about the token.
# sha256(token) → expiry_monotonic
_negative_cache: dict[str, float] = {}
_NEG_CACHE_TTL = 5.0
_NEG_CACHE_MAX = 4096


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Shared client: token validation runs on every MCP request, so reuse one
# connection pool instead of paying a new TCP handshake each time.
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=10.0, transport=instrumented_transport("hub")
        )
    return _client


def _evict(now: float) -> None:
    """Keep the cache bounded: drop expired entries, then oldest-expiring."""
    if len(_user_cache) < _CACHE_MAX:
        return
    for key in [k for k, (expiry, _) in _user_cache.items() if expiry <= now]:
        del _user_cache[key]
    while len(_user_cache) >= _CACHE_MAX:
        del _user_cache[min(_user_cache, key=lambda k: _user_cache[k][0])]


def _evict_negative(now: float) -> None:
    """Same bounding strategy for the negative cache (expiry-only values)."""
    if len(_negative_cache) < _NEG_CACHE_MAX:
        return
    for key in [k for k, expiry in _negative_cache.items() if expiry <= now]:
        del _negative_cache[key]
    while len(_negative_cache) >= _NEG_CACHE_MAX:
        del _negative_cache[min(_negative_cache, key=lambda k: _negative_cache[k])]


async def resolve_user(token: str) -> Optional[dict]:
    """Validate a JupyterHub Bearer token; return {username, namespace, token}.

    Returns None only when the Hub itself rejected the token. Raises
    HubUnavailable when the Hub could not be reached, or answered with
    anything other than a verdict on the token — that says nothing about
    the token and must not be reported as "invalid".

    Does not read Hub server ``state`` (admin-only). Tools that need to know
    whether a session is running query Hub ``servers[""].ready`` themselves,
    or filter Loki/Prometheus by ``username``.
    """
    now = time.monotonic()
    key = _hash_token(token)
    cached = _user_cache.get(key)
    if cached and now < cached[0]:
        record_auth("cache_hit")
        return cached[1]

    neg_expiry = _negative_cache.get(key)
    if neg_expiry and now < neg_expiry:
        record_auth("neg_cache_hit")
        return None

    try:
        resp = await _get_client().get(
            f"{HUB_API_URL}/user",
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.RequestError as exc:
        record_auth("hub_unreachable")
        raise HubUnavailable(
            f"JupyterHub API unreachable — {describe_exception(exc)}"
        ) from exc

    if resp.status_code in (401, 403):
        _evict_negative(now)
        _negative_cache[key] = now + _NEG_CACHE_TTL
        record_auth("invalid_token")
        return None

    if resp.status_code != 200:
        # 5xx: the Hub is broken; 404 and friends: this service is pointed
        # at the wrong place. Neither is the token's fault.
        record_auth("hub_error")
        detail = response_detail(resp, limit=120)
        problem = f"JupyterHub API returned HTTP {resp.status_code}"
        if resp.status_code == 404:
            problem += f" for {HUB_API_URL}/user (JUPYTERHUB_API_URL misconfigured?)"
        raise HubUnavailable(f"{problem} — {detail}" if detail else problem)

    data = json_body(resp)
    username = data.get("name") if isinstance(data, dict) else None
    if not isinstance(username, str) or not username:
        record_auth("hub_error")
        raise HubUnavailable(
            "JupyterHub API returned HTTP 200 but no user record for the token"
        )

    user_info = {
        "username": username,
        "namespace": NAMESPACE,
        "token": token,
    }
    _evict(now)
    _user_cache[key] = (now + _CACHE_TTL, user_info)
    record_auth("validated")
    return user_info


def clear_user_cache(token: str) -> None:
    """Remove a token's cached entries so the next request revalidates."""
    key = _hash_token(token)
    _user_cache.pop(key, None)
    _negative_cache.pop(key, None)
