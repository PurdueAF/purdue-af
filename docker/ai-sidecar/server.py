"""Purdue AF AI sidecar — MCP HTTP server.

Auth: JupyterHub Bearer token validated against the Hub API.
Tools are defined in tools/logs.py and tools/storage.py.
"""

import logging
import os
import time
from typing import Optional

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from tools import logs, storage

logger = logging.getLogger(__name__)

NB_USER = os.environ.get("NB_USER", "")
HUB_API_URL = os.environ.get("JUPYTERHUB_API_URL", "http://hub:8081/hub/api")

# token → (expiry_monotonic, username)
_token_cache: dict[str, tuple[float, str]] = {}
_TOKEN_TTL = 60.0


async def _validate_token(token: str) -> Optional[str]:
    now = time.monotonic()
    cached = _token_cache.get(token)
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

    username = resp.json().get("name")
    if username:
        _token_cache[token] = (now + _TOKEN_TTL, username)
    return username


class _AuthMiddleware:
    """Pure-ASGI middleware: validates JupyterHub Bearer tokens."""

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        # Silently reject anything that isn't the MCP endpoint (e.g. Prometheus /metrics).
        if not scope.get("path", "").startswith("/mcp"):
            await self._respond(send, 404, "not found")
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode()

        if not auth.startswith("Bearer "):
            await self._respond(send, 401, "Missing Bearer token")
            return

        token = auth[len("Bearer ") :]
        username = await _validate_token(token)

        if username is None:
            await self._respond(send, 401, "Invalid JupyterHub token")
            return

        if username != NB_USER:
            await self._respond(
                send, 403, f"Token is for '{username}', not this pod's user '{NB_USER}'"
            )
            return

        # Rewrite Host → localhost:9191 to satisfy the MCP SDK's DNS-rebinding
        # protection (it allows "localhost:*" by default).  Real auth is above.
        new_headers = [
            (b"host", b"localhost:9191") if k.lower() == b"host" else (k, v)
            for k, v in scope.get("headers", [])
        ]
        await self._app({**scope, "headers": new_headers}, receive, send)

    @staticmethod
    async def _respond(send, status: int, detail: str) -> None:
        body = f'{{"error":"{detail}"}}'.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _McpAccessFilter(logging.Filter):
    """Only keep uvicorn access-log entries for /mcp requests."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/mcp" in record.getMessage()


# ── MCP server setup ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "purdue-af-sidecar",
    stateless_http=True,
    instructions=(
        "Tools for the Purdue Analysis Facility user pod. "
        "Use query_notebook_logs / query_dask_logs for log queries; "
        "use query_storage_usage for disk quota information."
    ),
)

logs.register(mcp)
storage.register(mcp)


# ── entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logging.getLogger("uvicorn.access").addFilter(_McpAccessFilter())

    if not NB_USER:
        logger.warning("NB_USER is not set — token ownership check will always fail")

    app = _AuthMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=9191, log_level="info")


if __name__ == "__main__":
    main()
