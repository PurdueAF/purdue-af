"""Token validation and user-context resolution."""

import hashlib
import time
from typing import Optional

import httpx
from cachetools import TTLCache
from config import HUB_API_URL
from context import User
from errors import describe_exception, json_body, response_detail
from metrics import record_auth
from shared import shared_client


class HubUnavailable(Exception):
    """The Hub could not be asked whether a token is valid: the caller answers
    "try again" (503), never "your token is wrong" (401)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# Keyed by sha256 of the token, so a dump of the keys hands out no credential.
_CACHE_TTL = 60.0
_CACHE_MAX = 1024

# Tokens the Hub rejected; Hub failures are never cached, they say nothing of the token.
_NEG_CACHE_TTL = 5.0
_NEG_CACHE_MAX = 4096


def _now() -> float:
    # Looked up at call time so tests can drive the clock.
    return time.monotonic()


_user_cache: TTLCache[str, User] = TTLCache(_CACHE_MAX, _CACHE_TTL, timer=_now)
_negative_cache: TTLCache[str, bool] = TTLCache(
    _NEG_CACHE_MAX, _NEG_CACHE_TTL, timer=_now
)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def resolve_user(token: str) -> Optional[User]:
    """Validate a JupyterHub Bearer token; return the user it belongs to.

    Every caller of one token shares the returned dict, so copy it before
    handing it on. Returns None only when the Hub itself rejected the token. Raises
    HubUnavailable when the Hub could not be reached, or answered with
    anything other than a verdict on the token — that says nothing about
    the token and must not be reported as "invalid".
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
        resp = await shared_client("hub").get(
            f"{HUB_API_URL}/user",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
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
        # A broken Hub, or this service pointed at the wrong place: not the token.
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

    user: User = {"username": username, "token": token}
    _user_cache[key] = user
    record_auth("validated")
    return user
