"""Prometheus metrics for the agentic interface HTTP server.

Metric families:
  purdue_af_mcp_api_calls_total          HTTP requests by route/status
  purdue_af_mcp_jsonrpc_requests_total   MCP messages by JSON-RPC method/username
  purdue_af_mcp_tool_calls_total         tool invocations by tool/outcome/username
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
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

logger = logging.getLogger("agentic.tools")

API_CALLS_TOTAL = Counter(
    "purdue_af_mcp_api_calls_total",
    "Total HTTP requests to the Purdue AF MCP server",
    ["route", "status"],
)

JSONRPC_REQUESTS_TOTAL = Counter(
    "purdue_af_mcp_jsonrpc_requests_total",
    "MCP JSON-RPC messages received, by protocol method "
    "(tools/call, tools/list, initialize, notifications/…)",
    ["method", "username"],
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
    ["result"],  # cache_hit | validated | invalid_token | hub_unreachable
)


def record_request(route: str, status: int) -> None:
    API_CALLS_TOTAL.labels(route=route, status=str(status)).inc()


def record_jsonrpc(method: str, username: str) -> None:
    JSONRPC_REQUESTS_TOTAL.labels(method=method, username=username).inc()


def record_tool_call(tool: str, outcome: str, username: str) -> None:
    TOOL_CALLS_TOTAL.labels(tool=tool, outcome=outcome, username=username).inc()


def record_upstream(target: str, outcome: str, seconds: float) -> None:
    UPSTREAM_REQUESTS_TOTAL.labels(target=target, outcome=outcome).inc()
    UPSTREAM_DURATION.labels(target=target).observe(seconds)


def record_auth(result: str) -> None:
    AUTH_TOTAL.labels(result=result).inc()


# Substrings that mark a tool error string as a backend failure rather than
# a user mistake.  Matches the error-string conventions used across tools/
# ("… unreachable — …", "… returned HTTP …", "Error: HTTP …",
#  "… connection failed …", "Could not …", "… misconfigured …").
_UPSTREAM_MARKERS = (
    "unreachable",
    "connection failed",
    "returned http",
    "error: http",
    "could not",
    "misconfigured",
)


def tool_outcome(result: Any) -> str:
    """Classify a tool return value.

    Returns 'success', 'user_error' (bad input / nothing running), or
    'upstream_error' (a backend the tool depends on failed).  Exceptions are
    recorded separately as 'exception' by instrument_mcp.
    """
    if not isinstance(result, str):
        return "success"
    if not result.startswith(("Error", "Unknown ", "Could not")):
        return "success"
    lowered = result.lower()
    if any(marker in lowered for marker in _UPSTREAM_MARKERS):
        return "upstream_error"
    return "user_error"


def _username() -> str:
    user = current_user.get(None)
    return (user or {}).get("username") or "unknown"


def instrument_mcp(mcp) -> None:
    """Record metrics and a structured log line on every MCP tool invocation."""
    original_call_tool = mcp._tool_manager.call_tool

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

    async def call_tool(
        name: str,
        arguments: dict,
        context=None,
        convert_result: bool = False,
    ):
        username = _username()
        start = time.monotonic()
        try:
            result = await original_call_tool(
                name,
                arguments,
                context=context,
                convert_result=convert_result,
            )
        except Exception:
            _record(name, "exception", username, start)
            raise
        _record(name, tool_outcome(result), username, start)
        return result

    mcp._tool_manager.call_tool = call_tool


class _InstrumentedTransport(httpx.AsyncBaseTransport):
    """httpx transport wrapper that times outbound requests per backend target."""

    def __init__(
        self, target: Union[str, Callable[[httpx.Request], str]], **kwargs
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
    target: Union[str, Callable[[httpx.Request], str]], **kwargs
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
