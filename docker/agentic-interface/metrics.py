"""Prometheus metrics for the agentic interface HTTP server.

Metric families:
  purdue_af_mcp_api_calls_total          HTTP requests by route/status
  purdue_af_mcp_tool_calls_total         tool invocations by tool/outcome/username
                                         (success | needs_input | user_error |
                                          auth_error | upstream_error |
                                          exception — the errors.Failure
                                          subclass raised)
  purdue_af_mcp_tool_duration_seconds    tool invocation latency by tool
  purdue_af_mcp_upstream_requests_total  outbound backend requests by target/outcome
  purdue_af_mcp_upstream_duration_seconds  outbound backend latency by target
  purdue_af_mcp_auth_total               token validation results

The username label exists for ad-hoc per-user queries in Prometheus;
dashboards aggregate over it.
"""

import logging
import time
from typing import Any, Callable, Union

import httpx
from context import current_user
from errors import Failure, invalid_arguments, unexpected_failure
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import UrlElicitationRequiredError
from mcp.types import TextContent
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
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
    # cache_hit | neg_cache_hit | validated | invalid_token | hub_unreachable
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
    return (user or {}).get("username") or "unknown"


def _translate(name: str, username: str, exc: Exception) -> tuple[Exception, str]:
    """Decide what a tool's exception means: ``(exception to raise, outcome)``.

    Tools raise errors.Failure subclasses, which FastMCP wraps in a generic
    ToolError ("Error executing tool X: …") on the way out; unwrap those so
    the user reads our message. Anything else is either the caller's
    arguments (pydantic) or a fault in this service — the one place that sees
    the original exception, so it is logged with its traceback here.
    """
    if isinstance(exc, UrlElicitationRequiredError):
        # carries its own protocol semantics (error code -32042)
        return exc, "elicitation_required"
    cause = exc.__cause__ if isinstance(exc, ToolError) and exc.__cause__ else exc
    if isinstance(cause, Failure):
        logger.info("tool_call tool=%s user=%s failed: %s", name, username, cause)
        return cause, cause.outcome
    if isinstance(cause, ValidationError):
        logger.warning(
            "tool_call tool=%s user=%s invalid arguments: %s", name, username, cause
        )
        return invalid_arguments(name, cause), "user_error"
    if isinstance(exc, ToolError) and exc.__cause__ is None:
        # Raised deliberately by the SDK with a complete message (unknown tool).
        logger.warning("tool_call tool=%s user=%s refused: %s", name, username, exc)
        return exc, "user_error"
    logger.exception("tool_call tool=%s user=%s raised", name, username)
    return unexpected_failure(name, cause), "exception"


class InstrumentedFastMCP(FastMCP):
    """FastMCP that records metrics and a structured log line per tool call.

    ``call_tool`` is the SDK's public dispatch entry point (the low-level
    server calls it for every tools/call), so overriding it here needs no
    private attributes. It is also where a tool's NeedsChoices becomes an
    ordinary result: the help text is an instruction to the agent, not an
    error.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        username = _username()
        start = time.monotonic()
        try:
            result = await super().call_tool(name, arguments)
        except Exception as exc:
            cause = exc.__cause__ if isinstance(exc, ToolError) else None
            if isinstance(cause, NeedsChoices):
                self._record(name, "needs_input", username, start)
                return [TextContent(type="text", text=str(cause))]
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

    def __init__(
        self, target: Union[str, Callable[[httpx.Request], str]], **kwargs: Any
    ) -> None:
        self._target = target if callable(target) else (lambda request: target)
        self._inner = httpx.AsyncHTTPTransport(**kwargs)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = self._target(request)
        start = time.monotonic()
        try:
            response = await self._inner.handle_async_request(request)
        except Exception:
            record_upstream(target, "connection_error", time.monotonic() - start)
            raise
        outcome = "success" if response.status_code < 500 else "http_error"
        record_upstream(target, outcome, time.monotonic() - start)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def instrumented_transport(
    target: Union[str, Callable[[httpx.Request], str]], **kwargs: Any
) -> httpx.AsyncBaseTransport:
    """Build an httpx transport that records upstream metrics for `target`.

    `target` is either a fixed label or a callable deriving the label from the
    request (used when one client talks to several backends).  Extra kwargs
    (e.g. verify=) are passed to the underlying httpx.AsyncHTTPTransport.
    """
    return _InstrumentedTransport(target, **kwargs)


def metrics_body() -> bytes:
    return generate_latest()


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST
