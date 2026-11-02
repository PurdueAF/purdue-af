"""Prometheus metrics for the agentic interface HTTP server.

The tool-call username label exists for ad-hoc per-user queries in Prometheus;
dashboards aggregate over it.
"""

import logging
import time
from typing import Any

import httpx
from context import current_user
from errors import Failure, invalid_arguments, unexpected_failure
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError, UrlElicitationRequiredError
from mcp.types import CallToolResult, InputRequiredResult, TextContent
from prometheus_client import Counter, Histogram
from pydantic import ValidationError
from tools.elicitation import NeedsChoices

logger = logging.getLogger("agentic.tools")

API_CALLS_TOTAL = Counter(
    "purdue_af_mcp_api_calls_total",
    "Total HTTP requests to the Purdue AF MCP server",
    ["route", "status"],
)

TOOL_CALLS_TOTAL = Counter(
    "purdue_af_mcp_tool_calls_total",
    "Total MCP tool invocations",
    ["tool", "outcome", "username"],
)

TOOL_DURATION = Histogram(
    "purdue_af_mcp_tool_duration_seconds",
    "Wall-clock duration of MCP tool invocations",
    ["tool"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

UPSTREAM_REQUESTS_TOTAL = Counter(
    "purdue_af_mcp_upstream_requests_total",
    "Outbound requests from the MCP server to backend services",
    ["target", "outcome"],
)

UPSTREAM_DURATION = Histogram(
    "purdue_af_mcp_upstream_duration_seconds",
    "Duration of outbound requests to backend services",
    ["target"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

AUTH_TOTAL = Counter(
    "purdue_af_mcp_auth_total",
    "Bearer-token validation results",
    ["result"],
)


def record_request(route: str, status: int) -> None:
    API_CALLS_TOTAL.labels(route=route, status=str(status)).inc()


def record_tool_call(tool: str, outcome: str, username: str) -> None:
    TOOL_CALLS_TOTAL.labels(tool=tool, outcome=outcome, username=username).inc()


def record_upstream(target: str, outcome: str, seconds: float) -> None:
    UPSTREAM_REQUESTS_TOTAL.labels(target=target, outcome=outcome).inc()
    UPSTREAM_DURATION.labels(target=target).observe(seconds)


def record_auth(result: str) -> None:
    AUTH_TOTAL.labels(result=result).inc()


def _username() -> str:
    user = current_user.get(None)
    return user["username"] if user else "unknown"


def _translate(name: str, username: str, exc: ToolError) -> tuple[ToolError, str]:
    """Decide what a tool's exception means: ``(exception to raise, outcome)``.

    The SDK wraps a tool's errors.Failure in a generic ToolError ("Error
    executing tool X: …"); unwrap it so the user reads our message. Anything
    else is the caller's arguments (pydantic) or a fault in this service.
    """
    cause = exc.__cause__ or exc
    if isinstance(cause, Failure):
        logger.info("tool_call tool=%s user=%s failed: %s", name, username, cause)
        return cause, cause.outcome
    if isinstance(cause, ValidationError):
        logger.warning(
            "tool_call tool=%s user=%s invalid arguments: %s", name, username, cause
        )
        return invalid_arguments(name, cause), "user_error"
    if exc.__cause__ is None:
        # Raised deliberately by the SDK with a complete message (unknown tool).
        logger.warning("tool_call tool=%s user=%s refused: %s", name, username, exc)
        return exc, "user_error"
    logger.exception("tool_call tool=%s user=%s raised", name, username)
    return unexpected_failure(name, cause), "exception"


def _needs_input_result(text: str) -> CallToolResult:
    """Package NeedsChoices help text as an ordinary (non-error) tool result.

    Every tool is annotated ``-> str``, so the SDK declares an output schema
    ``{"result": <string>}`` for it, and clients reject a successful result
    without structured content that conforms, so both halves are returned.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={"result": text},
    )


class InstrumentedMCPServer(MCPServer):
    """MCPServer that records metrics and a structured log line per tool call,
    and turns a tool's NeedsChoices into an ordinary result: the help text is
    an instruction to the agent, not an error."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context | None = None
    ) -> CallToolResult | InputRequiredResult:
        username = _username()
        start = time.monotonic()
        try:
            result = await super().call_tool(name, arguments, context)
        except MCPError as exc:
            # A protocol error (UrlElicitationRequiredError is -32042) goes back as is.
            outcome = (
                "elicitation_required"
                if isinstance(exc, UrlElicitationRequiredError)
                else "exception"
            )
            self._record(name, outcome, username, start)
            raise
        except ToolError as exc:
            if isinstance(exc.__cause__, NeedsChoices):
                self._record(name, "needs_input", username, start)
                return _needs_input_result(str(exc.__cause__))
            failure, outcome = _translate(name, username, exc)
            self._record(name, outcome, username, start)
            if failure is exc:
                raise
            raise failure from None
        self._record(name, "success", username, start)
        return result

    @staticmethod
    def _record(name: str, outcome: str, username: str, start: float) -> None:
        elapsed = time.monotonic() - start
        TOOL_DURATION.labels(tool=name).observe(elapsed)
        record_tool_call(name, outcome, username)
        logger.info(
            "tool_call tool=%s user=%s outcome=%s duration_ms=%.0f",
            name,
            username,
            outcome,
            elapsed * 1000,
        )


class _InstrumentedTransport(httpx.AsyncBaseTransport):
    """httpx transport wrapper that times outbound requests per backend target."""

    def __init__(self, target: str, **kwargs: Any) -> None:
        self._target = target
        self._inner = httpx.AsyncHTTPTransport(**kwargs)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        start = time.monotonic()
        try:
            response = await self._inner.handle_async_request(request)
        except Exception:
            record_upstream(self._target, "connection_error", time.monotonic() - start)
            raise
        outcome = "success" if response.status_code < 500 else "http_error"
        record_upstream(self._target, outcome, time.monotonic() - start)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def instrumented_transport(target: str, **kwargs: Any) -> httpx.AsyncBaseTransport:
    """Build an httpx transport that records upstream metrics for `target`.

    Extra kwargs (e.g. verify=) are passed to the underlying
    httpx.AsyncHTTPTransport.
    """
    return _InstrumentedTransport(target, **kwargs)
