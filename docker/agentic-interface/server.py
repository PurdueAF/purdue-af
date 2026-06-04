"""Purdue AF Agentic Interface — JupyterHub Service MCP server.

Registered with JupyterHub as a service; accessible at
  https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp

Auth: incoming JupyterHub Bearer tokens are validated against the Hub API.
The resolved user identity (username + active pod name) is stored in a
ContextVar so tool functions can scope their queries per-request.
"""

import logging
import os
import time
from typing import Optional

import httpx
import uvicorn
from context import current_user
from mcp.server.fastmcp import FastMCP
from tools import logs, storage

logger = logging.getLogger(__name__)

HUB_API_URL = os.environ.get("JUPYTERHUB_API_URL", "http://hub:8081/hub/api")
NAMESPACE = os.environ.get("NAMESPACE", "cms")
# JupyterHub sets this for managed services; set it explicitly in the Deployment.
SERVICE_PREFIX = os.environ.get(
    "JUPYTERHUB_SERVICE_PREFIX", "/services/agentic-interface"
).rstrip("/")

# token → (expiry_monotonic, {username, pod_name, namespace})
_user_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 60.0


async def _resolve_user(token: str) -> Optional[dict]:
    """Validate a JupyterHub Bearer token and return the user context dict."""
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

    # Pod name lives in the default server's spawner state.
    pod_name = data.get("servers", {}).get("", {}).get("state", {}).get("pod_name", "")

    user_info = {"username": username, "pod_name": pod_name, "namespace": NAMESPACE}
    _user_cache[token] = (now + _CACHE_TTL, user_info)
    return user_info


class _PathStripper:
    """Strip SERVICE_PREFIX from request paths before forwarding to the MCP app.

    JupyterHub's proxy passes the full path (including /services/agentic-interface)
    to the service. The MCP app is mounted at /mcp, so we strip the prefix so it
    receives /mcp as expected.
    """

    def __init__(self, app, prefix: str) -> None:
        self._app = app
        self._prefix = prefix.rstrip("/")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.startswith(self._prefix):
                stripped = path[len(self._prefix) :] or "/"
                scope = {
                    **scope,
                    "path": stripped,
                    "root_path": scope.get("root_path", "") + self._prefix,
                }
        await self._app(scope, receive, send)


class _AuthMiddleware:
    """Validate JupyterHub Bearer tokens and populate the current_user ContextVar."""

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        # Only serve the MCP endpoint; return 404 for anything else.
        if not scope.get("path", "").startswith(f"{SERVICE_PREFIX}/mcp"):
            await self._respond(send, 404, "not found")
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode()

        if not auth.startswith("Bearer "):
            await self._respond(send, 401, "Missing Bearer token")
            return

        token = auth[len("Bearer ") :]
        user_info = await _resolve_user(token)

        if user_info is None:
            await self._respond(send, 401, "Invalid JupyterHub token")
            return

        # Rewrite Host → localhost:8888 to satisfy the MCP SDK's DNS-rebinding
        # protection.  Our own token check above is the real auth gate.
        new_headers = [
            (b"host", b"localhost:8888") if k.lower() == b"host" else (k, v)
            for k, v in scope.get("headers", [])
        ]

        # Bind user context for the duration of this request so tool functions
        # can call current_user.get() without needing extra arguments.
        ctx_token = current_user.set(user_info)
        try:
            await self._app({**scope, "headers": new_headers}, receive, send)
        finally:
            current_user.reset(ctx_token)

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
    def filter(self, record: logging.LogRecord) -> bool:
        return "/mcp" in record.getMessage()


# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "purdue-af-agentic-interface",
    stateless_http=True,
    instructions=(
        "Tools for the Purdue Analysis Facility. "
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

    if not os.environ.get("JUPYTERHUB_SERVICE_PREFIX"):
        logger.warning(
            "JUPYTERHUB_SERVICE_PREFIX is not set — defaulting to /services/agentic-interface"
        )

    inner = mcp.streamable_http_app()  # handles /mcp
    stripped = _PathStripper(
        inner, SERVICE_PREFIX
    )  # strips /services/agentic-interface
    app = _AuthMiddleware(stripped)  # validates Bearer token
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="info")


if __name__ == "__main__":
    main()
