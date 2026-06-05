"""Token validation and user-context resolution.

Extracted into its own module so that tool modules (e.g. session.py) can
call clear_user_cache() to force a fresh pod_name lookup after the session
state changes — without creating a circular import with server.py.
"""

import os
import time
from typing import Optional

import httpx

HUB_API_URL = os.environ.get("JUPYTERHUB_API_URL", "http://hub:8081/hub/api")
NAMESPACE = os.environ.get("NAMESPACE", "cms")

# token → (expiry_monotonic, user_info_dict)
_user_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 60.0


async def resolve_user(token: str) -> Optional[dict]:
    """Validate a JupyterHub Bearer token; return {username, pod_name, namespace, token}."""
    now = time.monotonic()
    cached = _user_cache.get(token)
    if cached and now < cached[0]:
        return cached[1]

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{HUB_API_URL}/user",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
        except httpx.RequestError:
            return None

    if resp.status_code != 200:
        return None

    data = resp.json()
    username = data.get("name")
    if not username:
        return None

    pod_name = data.get("servers", {}).get("", {}).get("state", {}).get("pod_name", "")

    user_info = {
        "username": username,
        "pod_name": pod_name,
        "namespace": NAMESPACE,
        "token": token,
    }
    _user_cache[token] = (now + _CACHE_TTL, user_info)
    return user_info


def clear_user_cache(token: str) -> None:
    """Remove a token's cached entry so the next request gets a fresh pod_name.

    Call this after a session starts or stops so tool functions that rely on
    current_user['pod_name'] see the updated state immediately.
    """
    _user_cache.pop(token, None)
