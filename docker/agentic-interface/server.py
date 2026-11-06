"""Purdue AF Agentic Interface — JupyterHub Service MCP server.

Registered with JupyterHub as a service; accessible at
  https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp

Auth: the middleware validates incoming JupyterHub Bearer tokens against the
Hub API and binds the user to a ContextVar, so tool functions scope their
queries per request.
"""

import json
import logging
import re
from collections.abc import Iterable

import scope_audit
import uvicorn
from auth import HubUnavailable, resolve_user
from config import SERVICE_PREFIX, SKILL_PATH, STATELESS_HTTP, TOKEN_URL
from context import current_user
from metrics import InstrumentedMCPServer, record_request
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tools import (
    dask,
    health,
    logs,
    metrics,
    profiles,
    prompts,
    promql,
    ray,
    session,
    storage,
)

logger = logging.getLogger(__name__)

# ── authentication failures ───────────────────────────────────────────────────

# A refused client shows only the status and this body, so it holds the diagnosis.

# A token that was never filled in: an unexpanded variable, a docs placeholder.
_PLACEHOLDER_RE = re.compile(
    r"^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|%[A-Za-z_][A-Za-z0-9_]*%|<[^>]*>"
    r"|YOUR[_-]?(?:API[_-]?)?TOKEN(?:[_-]?HERE)?|\.\.\.)$",
    re.IGNORECASE,
)

_HINT_MISSING = (
    "No Authorization header reached the service. Configure the MCP client to "
    "send 'Authorization: Bearer <JupyterHub API token>' — mint a token at "
    f"{TOKEN_URL}, or inside an AF session use the JUPYTERHUB_API_TOKEN the "
    "session provides."
)
_HINT_EMPTY = (
    "The Authorization header arrived as 'Bearer' with nothing after it: the "
    "environment variable or token file the MCP client reads (for example "
    "JUPYTERHUB_API_TOKEN, or ~/.config/purdue-af/token) is empty or unset. "
    "Fill it in, then reconnect the MCP server."
)
_HINT_INVALID = (
    "JupyterHub does not recognise this token: it is mistyped, expired, or was "
    "revoked (inside an AF session the token changes on every restart). Mint a "
    f"new one at {TOKEN_URL} — or, inside a session, restart the agent so it "
    "picks up the current JUPYTERHUB_API_TOKEN — then reconnect the MCP server."
)
_HINT_UNAVAILABLE = (
    "The token could not be checked because {detail}. This is a facility-side "
    "problem, not a token problem — try again in a minute."
)


def _token_problem(token: str) -> tuple[str, str] | None:
    """``(error, hint)`` for a credential that cannot be a token, else None."""
    if not token:
        return "Empty Bearer token", _HINT_EMPTY
    if _PLACEHOLDER_RE.match(token):
        return "Unexpanded token placeholder", (
            f"The token sent was the literal text {token[:40]!r}: the placeholder "
            "in the MCP client configuration was never replaced with a real "
            "token, or the environment variable it names is unset and the client "
            "did not expand it. Put a real token there (mint one at "
            f"{TOKEN_URL}), then reconnect the MCP server."
        )
    return None


async def _send(
    send: Send,
    status: int,
    body: bytes,
    content_type: bytes,
    headers: Iterable[tuple[bytes, bytes]] = (),
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode()),
                *headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _AuthMiddleware:
    """Validate JupyterHub Bearer tokens and populate the current_user ContextVar."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close"})
            return

        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        route = self._route_for(scope.get("path", ""))
        status = 500

        async def recording_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self._dispatch(route, scope, receive, recording_send)
        finally:
            record_request(route, status)

    async def _dispatch(
        self, route: str, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if route == "health":
            await _send(send, 200, b"ok", b"text/plain")
            return

        if route == "metrics":
            await _send(send, 200, generate_latest(), CONTENT_TYPE_LATEST.encode())
            return

        if route != "mcp":
            await self._respond(
                send,
                404,
                "not found",
                hint=f"The MCP endpoint is {SERVICE_PREFIX}/mcp.",
            )
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode(errors="replace").strip()

        if not auth:
            await self._reject(send, "Missing Bearer token", _HINT_MISSING)
            return

        scheme, _, token = auth.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer":
            await self._reject(
                send,
                "Unsupported Authorization scheme",
                f"The Authorization header used the scheme {scheme!r}; this "
                "service expects 'Authorization: Bearer <JupyterHub API token>'. "
                "(JupyterHub's own REST API takes 'token <…>', but the MCP "
                "endpoint does not.)",
            )
            return

        problem = _token_problem(token)
        if problem is not None:
            await self._reject(send, *problem, invalid=True)
            return

        try:
            user = await resolve_user(token)
        except HubUnavailable as exc:
            logger.warning("token validation impossible: %s", exc.detail)
            await self._respond(
                send,
                503,
                "JupyterHub API unavailable",
                hint=_HINT_UNAVAILABLE.format(detail=exc.detail),
                extra_headers=[(b"retry-after", b"10")],
            )
            return

        if user is None:
            await self._reject(
                send, "Invalid JupyterHub token", _HINT_INVALID, invalid=True
            )
            return

        # The SDK's DNS-rebinding protection wants this Host; the token check is the gate.
        new_headers = [
            (b"host", b"localhost:8888") if k.lower() == b"host" else (k, v)
            for k, v in scope.get("headers", [])
        ]

        ctx_token = current_user.set(user.copy())
        try:
            await self._app({**scope, "headers": new_headers}, receive, send)
        finally:
            current_user.reset(ctx_token)

    @staticmethod
    def _route_for(path: str) -> str:
        if path in ("/health", f"{SERVICE_PREFIX}/health"):
            return "health"
        # Only unprefixed: the proxied form is public and the metrics carry usernames.
        if path == "/metrics":
            return "metrics"
        if path.startswith(f"{SERVICE_PREFIX}/mcp"):
            return "mcp"
        return "other"

    @classmethod
    async def _reject(
        cls, send: Send, error: str, hint: str, *, invalid: bool = False
    ) -> None:
        """401 with the diagnosis in the body and an RFC 6750 challenge header.

        ``invalid`` marks a credential that was presented but unusable; a
        request that carried no usable credential at all gets the bare
        challenge, as the RFC asks.
        """
        logger.info("rejected request: %s", error)
        challenge = 'Bearer realm="purdue-af-agentic-interface"'
        if invalid:
            description = hint.replace('"', "'")
            challenge += f', error="invalid_token", error_description="{description}"'
        await cls._respond(
            send,
            401,
            error,
            hint=hint,
            extra_headers=[(b"www-authenticate", challenge.encode())],
        )

    @staticmethod
    async def _respond(
        send: Send,
        status: int,
        detail: str,
        *,
        hint: str | None = None,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        payload: dict[str, str] = {"error": detail}
        if hint:
            payload["hint"] = hint
        await _send(
            send,
            status,
            json.dumps(payload).encode(),
            b"application/json",
            extra_headers or (),
        )


class _McpAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/mcp" in record.getMessage()


# ── MCP server ────────────────────────────────────────────────────────────────


def skill_version(text: str) -> str:
    """The MAJOR.MINOR the skill states it ships in."""
    m = re.search(r"(?m)^Skill version: `(\d+\.\d+)`$", text)
    if not m:
        raise ValueError("the skill states no 'Skill version: `X.Y`'")
    return m.group(1)


SKILL_VERSION = skill_version(open(SKILL_PATH).read())

mcp = InstrumentedMCPServer(
    "purdue-af-agentic-interface",
    version=SKILL_VERSION,
    instructions=(
        "Tools for the Purdue Analysis Facility. "
        "Use query_notebook_logs / query_dask_logs for log queries; "
        "use query_storage_usage for disk quota information; "
        'use get_facility_health for "is the AF healthy / is something broken"; '
        'use get_facility_metrics for "how busy is the AF" and other dashboard '
        "numbers; "
        "use list_prometheus_metrics / query_prometheus for any other metric, "
        "PromQL over the facility's monitoring; "
        "use list_dask_clusters / list_dask_cluster_options / create_dask_cluster / "
        "get_dask_worker_count / get_dask_cluster_usage / scale_dask_cluster / "
        "stop_dask_cluster for Dask; "
        "use get_ray_capacity / submit_ray_job / list_ray_jobs / get_ray_job / "
        "stop_ray_job / delete_ray_job for Ray jobs on GPUs, and "
        "get_ray_cluster / start_ray_cluster / stop_ray_cluster for the user's "
        "own Ray cluster; "
        "use get_session_status / start_af_session / stop_af_session for pod lifecycle. "
        "Each tool result names the next step. "
        f"The current purdue-af-agentic-interface skill is version {SKILL_VERSION}, "
        "this server's MAJOR.MINOR (a patch release never changes the skill). "
        "If the copy you loaded states another version, or you have none, tell "
        "the user once, naming both versions: on their own machine they "
        "reinstall it as in "
        "https://analysis-facility.physics.purdue.edu/guide-agentic-interface/"
        "#installing-the-skill-recommended; inside an AF session it comes with "
        "the session image and updates when the session restarts on a newer one."
    ),
)

logs.register(mcp)
storage.register(mcp)
health.register(mcp)
dask.register(mcp)
ray.register(mcp)
metrics.register(mcp)
promql.register(mcp)
profiles.register(mcp)
session.register(mcp)
prompts.register(mcp)


# ── entry point ───────────────────────────────────────────────────────────────


def build_app() -> ASGIApp:
    """Bearer-token validation in front of the MCP app, which serves the full
    path JupyterHub's proxy passes on, service prefix included."""
    return _AuthMiddleware(
        mcp.streamable_http_app(
            streamable_http_path=f"{SERVICE_PREFIX}/mcp",
            stateless_http=STATELESS_HTTP,
        )
    )


def main() -> None:
    # force: MCPServer() already configured the root logger at import time.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    logging.getLogger("uvicorn.access").addFilter(_McpAccessFilter())
    scope_audit.start()
    uvicorn.run(build_app(), host="0.0.0.0", port=8888, log_level="info")


if __name__ == "__main__":
    main()
