"""Prometheus metrics for the agentic interface HTTP server."""

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


def instrument_mcp(mcp) -> None:
    """Record tool metrics on every MCP tool invocation."""
    original_call_tool = mcp._tool_manager.call_tool

    async def call_tool(
        name: str,
        arguments: dict,
        context=None,
        convert_result: bool = False,
    ):
        try:
            result = await original_call_tool(
                name,
                arguments,
                context=context,
                convert_result=convert_result,
            )
        except Exception:
            record_tool_call(name, "error")
            raise
        record_tool_call(name, tool_outcome(result))
        return result

    mcp._tool_manager.call_tool = call_tool


def metrics_body() -> bytes:
    return generate_latest()


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST
