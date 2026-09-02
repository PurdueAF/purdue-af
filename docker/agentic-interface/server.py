"""Purdue AF Agentic Interface — JupyterHub Service MCP server.

Registered with JupyterHub as a service; accessible at
  https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp

Auth: incoming JupyterHub Bearer tokens are validated against the Hub API
through the MCP SDK's TokenVerifier protocol (auth.HubTokenVerifier). The
resolved user identity (username) is stored in a ContextVar so tool
functions can scope their queries per-request.
"""

import json
import logging
import re

import uvicorn
from auth import HubTokenVerifier, HubUnavailable
from config import NAMESPACE, SERVICE_PREFIX, STATELESS_HTTP, TOKEN_URL
from context import current_user
from metrics import (
    InstrumentedFastMCP,
    metrics_body,
    metrics_content_type,
    record_request,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tools import dask, health, logs, profiles, prompts, session, storage

logger = logging.getLogger(__name__)

# ── authentication failures ───────────────────────────────────────────────────
#
# An MCP client that is refused here shows the user nothing but the HTTP
# status and (at best) this body, so the body must carry the whole diagnosis:
# what was wrong with the credential and what to do about it. Nothing else
# documents these messages — they are the single source of truth.

# What a client sends when the token it was configured with was never filled
# in: an unexpanded ${VAR}/$VAR/%VAR%, or a placeholder copied from the docs.
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


# Module-level so tests can substitute a verifier.
verify_token = HubTokenVerifier().verify_token


def _token_problem(token: str) -> tuple[str, str] | None:
    """Reject credentials that cannot be a token before asking the Hub.

    Returns ``(error, hint)`` or None. Catching these here turns a generic
    "invalid token" into the actual mistake: an unset variable, a placeholder
    that was never replaced.
    """
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


class _AuthMiddleware:
    """Validate JupyterHub Bearer tokens and populate the current_user ContextVar."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Streamable HTTP never uses websockets — refuse the handshake outright
        # rather than fall through to HTTP route logic (whose _respond would
        # send http.response messages on a websocket scope).
        if scope["type"] == "websocket":
            await send({"type": "websocket.close"})
            return

        if scope["type"] != "http":
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
            await self._respond(
                send,
                404,
                "not found",
                hint=f"The MCP endpoint is {SERVICE_PREFIX}/mcp.",
            )
            record_request(route, 404)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode(errors="replace").strip()

        if not auth:
            await self._reject(send, route, "Missing Bearer token", _HINT_MISSING)
            return

        scheme, _, token = auth.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer":
            await self._reject(
                send,
                route,
                "Unsupported Authorization scheme",
                f"The Authorization header used the scheme {scheme!r}; this "
                "service expects 'Authorization: Bearer <JupyterHub API token>'. "
                "(JupyterHub's own REST API takes 'token <…>', but the MCP "
                "endpoint does not.)",
            )
            return

        problem = _token_problem(token)
        if problem is not None:
            await self._reject(send, route, *problem, invalid=True)
            return

        try:
            access = await verify_token(token)
        except HubUnavailable as exc:
            logger.warning("token validation impossible: %s", exc.detail)
            await self._respond(
                send,
                503,
                "JupyterHub API unavailable",
                hint=_HINT_UNAVAILABLE.format(detail=exc.detail),
                extra_headers=[(b"retry-after", b"10")],
            )
            record_request(route, 503)
            return

        if access is None:
            await self._reject(
                send, route, "Invalid JupyterHub token", _HINT_INVALID, invalid=True
            )
            return
        user_info = {
            "username": access.client_id,
            "namespace": NAMESPACE,
            "token": access.token,
        }

        # Rewrite Host → localhost:8888 to satisfy the MCP SDK's DNS-rebinding
        # protection.  Our own token check above is the real auth gate.
        new_headers = [
            (b"host", b"localhost:8888") if k.lower() == b"host" else (k, v)
            for k, v in scope.get("headers", [])
        ]

        # Bind user context for the duration of this request so tool functions
        # can call current_user.get() without needing extra arguments.
        status = 500

        async def counting_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        ctx_token = current_user.set(user_info)
        try:
            await self._app({**scope, "headers": new_headers}, receive, counting_send)
        finally:
            current_user.reset(ctx_token)
            record_request(route, status)

    @staticmethod
    def _route_for(path: str) -> str:
        if path in ("/health", f"{SERVICE_PREFIX}/health"):
            return "health"
        # Prometheus scrapes the pod directly (Service label scrape-metrics,
        # unprefixed path). The proxied ${SERVICE_PREFIX}/metrics form would be
        # publicly reachable and the metric families carry username labels, so
        # only the exact unprefixed path is served; the prefixed form is 404.
        if path == "/metrics":
            return "metrics"
        if path.startswith(f"{SERVICE_PREFIX}/mcp"):
            return "mcp"
        return "other"

    @staticmethod
    async def _ok(send: Send) -> None:
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
    async def _metrics(send: Send) -> None:
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

    @classmethod
    async def _reject(
        cls, send: Send, route: str, error: str, hint: str, *, invalid: bool = False
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
        record_request(route, 401)

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
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *(extra_headers or []),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _McpAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/mcp" in record.getMessage()


# ── MCP server ────────────────────────────────────────────────────────────────

# JupyterHub's proxy passes the full path (including the service prefix) to
# the service, so the MCP app is told to serve exactly that path.
mcp = InstrumentedFastMCP(
    "purdue-af-agentic-interface",
    stateless_http=STATELESS_HTTP,
    streamable_http_path=f"{SERVICE_PREFIX}/mcp",
    instructions=(
        "Tools for the Purdue Analysis Facility. "
        "Use query_notebook_logs / query_dask_logs for log queries; "
        "use query_storage_usage for disk quota information; "
        'use get_facility_health for "is the AF healthy / is something broken"; '
        "use list_dask_clusters / list_dask_cluster_options / create_dask_cluster / "
        "get_dask_worker_count / get_dask_cluster_usage / scale_dask_cluster / "
        "stop_dask_cluster for Dask; "
        "use get_session_status / start_af_session / stop_af_session for pod lifecycle. "
        "Each tool result names the next step."
    ),
)

logs.register(mcp)
storage.register(mcp)
health.register(mcp)
dask.register(mcp)
profiles.register(mcp)
session.register(mcp)
prompts.register(mcp)


# ── entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logging.getLogger("uvicorn.access").addFilter(_McpAccessFilter())

    # Bearer-token validation in front of the MCP app, which serves
    # {SERVICE_PREFIX}/mcp itself.
    app = _AuthMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="info")


if __name__ == "__main__":
    main()
