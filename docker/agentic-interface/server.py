"""Purdue AF Agentic Interface — JupyterHub Service MCP server.

Registered with JupyterHub as a service; accessible at
  https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp

Auth: incoming JupyterHub Bearer tokens are validated against the Hub API.
The resolved user identity (username + active pod name) is stored in a
ContextVar so tool functions can scope their queries per-request.
"""

import logging
import os

import uvicorn
from auth import resolve_user
from context import current_user
from metrics import instrument_mcp, metrics_body, metrics_content_type, record_request
from mcp.server.fastmcp import FastMCP
from tools import connect, dask, logs, profiles, prompts, session, storage

logger = logging.getLogger(__name__)

SERVICE_PREFIX = os.environ.get(
    "JUPYTERHUB_SERVICE_PREFIX", "/services/agentic-interface"
).rstrip("/")


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

        path = scope.get("path", "")
        route = self._route_for(path)

        # Unauthenticated liveness/readiness probe. The kubelet hits the pod
        # directly at /health (no JupyterHub service prefix); accept the
        # prefixed form too in case it is probed through the proxy.
        if route == "health":
            await self._ok(send)
            record_request(route, 200)
            return

        # Unauthenticated Prometheus scrape endpoint.
        if route == "metrics":
            await self._metrics(send)
            record_request(route, 200)
            return

        # Only serve the MCP endpoint; return 404 for anything else.
        if route != "mcp":
            await self._respond(send, 404, "not found")
            record_request(route, 404)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode()

        if not auth.startswith("Bearer "):
            await self._respond(send, 401, "Missing Bearer token")
            record_request(route, 401)
            return

        token = auth[len("Bearer ") :]
        user_info = await resolve_user(token)

        if user_info is None:
            await self._respond(send, 401, "Invalid JupyterHub token")
            record_request(route, 401)
            return

        # Rewrite Host → localhost:8888 to satisfy the MCP SDK's DNS-rebinding
        # protection.  Our own token check above is the real auth gate.
        new_headers = [
            (b"host", b"localhost:8888") if k.lower() == b"host" else (k, v)
            for k, v in scope.get("headers", [])
        ]

        # Bind user context for the duration of this request so tool functions
        # can call current_user.get() without needing extra arguments.
        status = 500

        async def counting_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        ctx_token = current_user.set(user_info)
        try:
            await self._app(
                {**scope, "headers": new_headers}, receive, counting_send
            )
        finally:
            current_user.reset(ctx_token)
            record_request(route, status)

    @staticmethod
    def _route_for(path: str) -> str:
        if path in ("/health", f"{SERVICE_PREFIX}/health"):
            return "health"
        if path in ("/metrics", f"{SERVICE_PREFIX}/metrics"):
            return "metrics"
        if path.startswith(f"{SERVICE_PREFIX}/mcp"):
            return "mcp"
        return "other"

    @staticmethod
    async def _ok(send) -> None:
        body = b"ok"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/plain"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _metrics(send) -> None:
        body = metrics_body()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", metrics_content_type().encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

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
        "use query_storage_usage for disk quota information; "
        "use list_dask_clusters / scale_dask_cluster / stop_dask_cluster for Dask; "
        "use get_session_status / start_af_session / stop_af_session for pod lifecycle; "
        "to connect over SSH, call prepare_ssh_connection then connect_to_session. "
        "Each tool result names the next step. Invocable workflow prompts "
        "(launch/connect/restart/stop) are also available."
    ),
)

instrument_mcp(mcp)

logs.register(mcp)
storage.register(mcp)
dask.register(mcp)
profiles.register(mcp)
session.register(mcp)
connect.register(mcp)
prompts.register(mcp)


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
