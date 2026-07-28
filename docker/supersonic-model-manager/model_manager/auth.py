"""HTTP Basic auth applied to the whole app except unauthenticated probes."""

import base64
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from .config import settings

PUBLIC_PATHS = ("/healthz", "/readyz")

_UNAUTHORIZED_HEADERS = {
    "WWW-Authenticate": 'Basic realm="SuperSONIC Model Manager", charset="UTF-8"'
}


def _check(header: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8")
    except Exception:
        return False
    username, _, password = decoded.partition(":")
    # compare_digest on both fields so timing does not leak either one
    user_ok = secrets.compare_digest(username, settings.auth_username)
    pass_ok = secrets.compare_digest(password, settings.auth_password)
    return user_ok and pass_ok


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next) -> Response:
        if not settings.auth_enabled or request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        if not settings.auth_password:
            return JSONResponse(
                {
                    "error": "Authentication is enabled but no password is configured. "
                    "Set AUTH_PASSWORD (or disable auth with AUTH_ENABLED=false)."
                },
                status_code=500,
            )

        if not _check(request.headers.get("authorization", "")):
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers=_UNAUTHORIZED_HEADERS,
            )

        return await call_next(request)
