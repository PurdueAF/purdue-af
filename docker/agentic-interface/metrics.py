"""Prometheus metrics for the agentic interface HTTP server."""

import functools
import inspect
from collections.abc import Callable
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

API_CALLS_TOTAL = Counter(
    "purdue_af_mcp_api_calls_total",
    "Total HTTP requests to the Purdue AF MCP server",
    ["route", "status"],
)

TOOL_CALLS_TOTAL = Counter(
    "purdue_af_mcp_tool_calls_total",
    "Total MCP tool invocations",
    ["tool", "outcome"],
)


def record_request(route: str, status: int) -> None:
    API_CALLS_TOTAL.labels(route=route, status=str(status)).inc()


def record_tool_call(tool: str, outcome: str) -> None:
    TOOL_CALLS_TOTAL.labels(tool=tool, outcome=outcome).inc()


def tool_outcome(result: Any) -> str:
    """Classify a tool return value as success or error."""
    if isinstance(result, str) and result.startswith("Error"):
        return "error"
    return "success"


def _wrap_tool_fn(fn: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            try:
                result = await fn(*args, **kwargs)
            except Exception:
                record_tool_call(tool_name, "error")
                raise
            record_tool_call(tool_name, tool_outcome(result))
            return result

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception:
            record_tool_call(tool_name, "error")
            raise
        record_tool_call(tool_name, tool_outcome(result))
        return result

    return sync_wrapper


def instrument_mcp(mcp) -> None:
    """Wrap mcp.add_tool so every registered tool records call metrics."""
    original_add_tool = mcp.add_tool

    def add_tool(fn, *args, **kwargs):
        tool_name = kwargs.get("name") or fn.__name__
        return original_add_tool(_wrap_tool_fn(fn, tool_name), *args, **kwargs)

    mcp.add_tool = add_tool


def metrics_body() -> bytes:
    return generate_latest()


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST
