"""Token validation and user-context resolution.

Extracted into its own module so that tool modules (e.g. session.py) can
call clear_user_cache() after session state changes — without creating a
circular import with server.py.

``HubTokenVerifier`` is the MCP SDK's ``TokenVerifier`` protocol backed by
the Hub: the middleware validates through it, and it is the seam the SDK's
own auth stack (``FastMCP(token_verifier=…)``) would plug into.
"""

import hashlib
import os
import time
from typing import Optional

import httpx
from cachetools import TTLCache
from errors import describe_exception, json_body, response_detail
from mcp.server.auth.provider import AccessToken
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
_CACHE_TTL = 60.0
_CACHE_MAX = 1024

# Negative cache: tokens the Hub actively rejected. Short TTL so a garbage-
# token flood doesn't amplify into a live Hub API call per request, while a
# freshly issued token is never locked out for long. Hub failures are
# deliberately NOT cached — they say nothing about the token.
_NEG_CACHE_TTL = 5.0
_NEG_CACHE_MAX = 4096


def _now() -> float:
    # Looked up at call time so tests can drive the clock.
    return time.monotonic()


# TTLCache drops expired entries first and least-recently-used ones once
# full, which is exactly the bounding both caches need.
# sha256(token) → user_info_dict
_user_cache: TTLCache[str, dict] = TTLCache(_CACHE_MAX, _CACHE_TTL, timer=_now)
# sha256(token) → True
_negative_cache: TTLCache[str, bool] = TTLCache(
    _NEG_CACHE_MAX, _NEG_CACHE_TTL, timer=_now
)


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
    key = _hash_token(token)
    cached = _user_cache.get(key)
    if cached is not None:
        record_auth("cache_hit")
        return cached

    if key in _negative_cache:
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
        _negative_cache[key] = True
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
    _user_cache[key] = user_info
    record_auth("validated")
    return user_info


class HubTokenVerifier:
    """``mcp.server.auth.provider.TokenVerifier`` backed by the Hub.

    ``client_id`` carries the Hub username — the SDK's AccessToken has no
    other identity field, and the middleware maps it back into the user
    context tools read. Raises HubUnavailable when no verdict is possible.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        user = await resolve_user(token)
        if user is None:
            return None
        return AccessToken(token=token, client_id=user["username"], scopes=[])


def clear_user_cache(token: str) -> None:
    """Remove a token's cached entries so the next request revalidates."""
    key = _hash_token(token)
    _user_cache.pop(key, None)
    _negative_cache.pop(key, None)
