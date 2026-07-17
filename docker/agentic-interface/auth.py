"""Token validation and user-context resolution.

Extracted into its own module so that tool modules (e.g. session.py) can
call clear_user_cache() after session state changes — without creating a
circular import with server.py.
"""

import os
import time
from typing import Optional

import httpx
from metrics import instrumented_transport, record_auth

HUB_API_URL = os.environ.get("JUPYTERHUB_API_URL", "http://hub:8081/hub/api")
NAMESPACE = os.environ.get("NAMESPACE", "cms")

# token → (expiry_monotonic, user_info_dict)
_user_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 60.0
_CACHE_MAX = 1024

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
    for tok in [t for t, (expiry, _) in _user_cache.items() if expiry <= now]:
        del _user_cache[tok]
    while len(_user_cache) >= _CACHE_MAX:
        del _user_cache[min(_user_cache, key=lambda t: _user_cache[t][0])]


async def resolve_user(token: str) -> Optional[dict]:
    """Validate a JupyterHub Bearer token; return {username, namespace, token}.

    Does not read Hub server ``state`` (admin-only). Tools that need to know
    whether a session is running query Hub ``servers[""].ready`` themselves,
    or filter Loki/Prometheus by ``username``.
    """
    now = time.monotonic()
    cached = _user_cache.get(token)
    if cached and now < cached[0]:
        record_auth("cache_hit")
        return cached[1]

    try:
        resp = await _get_client().get(
            f"{HUB_API_URL}/user",
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.RequestError:
        record_auth("hub_unreachable")
        return None

    if resp.status_code != 200:
        record_auth("invalid_token")
        return None

    data = resp.json()
    username = data.get("name")
    if not username:
        record_auth("invalid_token")
        return None

    user_info = {
        "username": username,
        "namespace": NAMESPACE,
        "token": token,
    }
    _evict(now)
    _user_cache[token] = (now + _CACHE_TTL, user_info)
    record_auth("validated")
    return user_info


def clear_user_cache(token: str) -> None:
    """Remove a token's cached entry so the next request revalidates."""
    _user_cache.pop(token, None)
