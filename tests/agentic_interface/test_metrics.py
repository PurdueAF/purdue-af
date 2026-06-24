"""Tests for Prometheus metrics (metrics.py + /metrics endpoint)."""

import metrics
import pytest
import server
from prometheus_client import REGISTRY


def _counter_value(route: str, status: str) -> float:
    return REGISTRY.get_sample_value(
        "agentic_interface_api_calls_total",
        {"route": route, "status": status},
    ) or 0.0


async def noop_receive():  # pragma: no cover
    return {"type": "http.request"}


class SendCollector:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return self.messages[0]["status"]

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.messages[1:])


def http_scope(path, headers=None):
    return {
        "type": "http",
        "path": path,
        "headers": headers or [],
    }


async def test_metrics_endpoint_returns_prometheus_format():
    send = SendCollector()
    before = _counter_value("metrics", "200")

    await server._AuthMiddleware(server._PathStripper(lambda *a: None, server.SERVICE_PREFIX))(
        http_scope("/metrics"), noop_receive, send
    )

    assert send.status == 200
    assert b"agentic_interface_api_calls_total" in send.body
    assert b"agentic_interface_tool_calls_total" in send.body
    assert _counter_value("metrics", "200") == before + 1


async def test_mcp_request_increments_api_call_counter(monkeypatch):
    async def accept(token):
        return {"username": "alice", "pod_name": "pod-a", "namespace": "cms", "token": "t"}

    monkeypatch.setattr(server, "resolve_user", accept)

    class Inner:
        calls = 0

        async def __call__(self, scope, receive, send):
            Inner.calls += 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    inner = Inner()
    app = server._AuthMiddleware(server._PathStripper(inner, server.SERVICE_PREFIX))
    before = _counter_value("mcp", "200")

    await app(
        http_scope(
            f"{server.SERVICE_PREFIX}/mcp",
            headers=[(b"authorization", b"Bearer good"), (b"host", b"hub:9999")],
        ),
        noop_receive,
        SendCollector(),
    )

    assert inner.calls == 1
    assert _counter_value("mcp", "200") == before + 1


async def test_unauthorized_mcp_request_records_401(monkeypatch):
    before = _counter_value("mcp", "401")
    send = SendCollector()

    await server._AuthMiddleware(server._PathStripper(lambda *a: None, server.SERVICE_PREFIX))(
        http_scope(f"{server.SERVICE_PREFIX}/mcp"), noop_receive, send
    )

    assert send.status == 401
    assert _counter_value("mcp", "401") == before + 1


def test_record_request_increments_counter():
    before = _counter_value("other", "418")
    metrics.record_request("other", 418)
    assert _counter_value("other", "418") == before + 1


def _tool_counter_value(tool: str, outcome: str) -> float:
    return REGISTRY.get_sample_value(
        "agentic_interface_tool_calls_total",
        {"tool": tool, "outcome": outcome},
    ) or 0.0


def test_tool_outcome_classifier():
    assert metrics.tool_outcome("ok") == "success"
    assert metrics.tool_outcome("Error: no running server") == "error"


async def test_instrumented_tool_records_success():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-metrics")
    metrics.instrument_mcp(mcp)

    @mcp.tool()
    async def hello() -> str:
        return "hi"

    tool = mcp._tool_manager._tools["hello"]
    before = _tool_counter_value("hello", "success")

    assert await tool.run({}) == "hi"
    assert _tool_counter_value("hello", "success") == before + 1


async def test_instrumented_tool_records_string_error():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-metrics")
    metrics.instrument_mcp(mcp)

    @mcp.tool()
    async def fail_soft() -> str:
        return "Error: something went wrong"

    tool = mcp._tool_manager._tools["fail_soft"]
    before = _tool_counter_value("fail_soft", "error")

    out = await tool.run({})

    assert out.startswith("Error:")
    assert _tool_counter_value("fail_soft", "error") == before + 1


async def test_instrumented_tool_records_exception():
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    mcp = FastMCP("test-metrics")
    metrics.instrument_mcp(mcp)

    @mcp.tool()
    async def fail_hard() -> str:
        raise RuntimeError("boom")

    tool = mcp._tool_manager._tools["fail_hard"]
    before = _tool_counter_value("fail_hard", "error")

    with pytest.raises(ToolError):
        await tool.run({})

    assert _tool_counter_value("fail_hard", "error") == before + 1
