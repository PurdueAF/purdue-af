"""Tests for Prometheus metrics (metrics.py + /metrics endpoint)."""

import json

import httpx
import metrics
import pytest
import respx
import server
from context import current_user
from prometheus_client import REGISTRY


def _counter_value(route: str, status: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "purdue_af_mcp_api_calls_total",
            {"route": route, "status": status},
        )
        or 0.0
    )


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


def http_scope(path, headers=None, method=None):
    scope = {
        "type": "http",
        "path": path,
        "headers": headers or [],
    }
    if method:
        scope["method"] = method
    return scope


async def test_metrics_endpoint_returns_prometheus_format():
    send = SendCollector()
    before = _counter_value("metrics", "200")

    await server._AuthMiddleware(
        server._PathStripper(lambda *a: None, server.SERVICE_PREFIX)
    )(http_scope("/metrics"), noop_receive, send)

    assert send.status == 200
    assert b"purdue_af_mcp_api_calls_total" in send.body
    assert b"purdue_af_mcp_tool_calls_total" in send.body
    assert _counter_value("metrics", "200") == before + 1


async def test_mcp_request_increments_api_call_counter(monkeypatch):
    async def accept(token):
        return {
            "username": "alice",
            "namespace": "cms",
            "token": "t",
        }

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

    await server._AuthMiddleware(
        server._PathStripper(lambda *a: None, server.SERVICE_PREFIX)
    )(http_scope(f"{server.SERVICE_PREFIX}/mcp"), noop_receive, send)

    assert send.status == 401
    assert _counter_value("mcp", "401") == before + 1


def test_record_request_increments_counter():
    before = _counter_value("other", "418")
    metrics.record_request("other", 418)
    assert _counter_value("other", "418") == before + 1


# ── JSON-RPC method breakdown ─────────────────────────────────────────────────


def _jsonrpc_counter_value(method: str, username: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "purdue_af_mcp_jsonrpc_requests_total",
            {"method": method, "username": username},
        )
        or 0.0
    )


def test_jsonrpc_methods_single_request():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}).encode()
    assert server._jsonrpc_methods(body) == ["tools/call"]


def test_jsonrpc_methods_batch():
    body = json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
    ).encode()
    assert server._jsonrpc_methods(body) == [
        "initialize",
        "notifications/initialized",
    ]


def test_jsonrpc_methods_response_and_invalid():
    # A client-to-server response carries no method field.
    assert server._jsonrpc_methods(b'{"jsonrpc":"2.0","id":1,"result":{}}') == [
        "response"
    ]
    assert server._jsonrpc_methods(b"not json") == ["invalid"]
    assert server._jsonrpc_methods(b"") == ["invalid"]


async def test_post_mcp_request_records_jsonrpc_method(monkeypatch):
    async def accept(token):
        return {
            "username": "alice",
            "namespace": "cms",
            "token": "t",
        }

    monkeypatch.setattr(server, "resolve_user", accept)

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    supplied = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return supplied.pop(0)

    seen_body = b""

    async def inner(scope, receive, send):
        # The replayed receive must hand the buffered body through unchanged.
        nonlocal seen_body
        message = await receive()
        seen_body = message.get("body", b"")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = server._AuthMiddleware(server._PathStripper(inner, server.SERVICE_PREFIX))
    before = _jsonrpc_counter_value("tools/list", "alice")

    await app(
        http_scope(
            f"{server.SERVICE_PREFIX}/mcp",
            headers=[(b"authorization", b"Bearer good"), (b"host", b"hub:9999")],
            method="POST",
        ),
        receive,
        SendCollector(),
    )

    assert seen_body == body
    assert _jsonrpc_counter_value("tools/list", "alice") == before + 1


# ── tool-call metrics ─────────────────────────────────────────────────────────


def _tool_counter_value(tool: str, outcome: str, username: str = "unknown") -> float:
    return (
        REGISTRY.get_sample_value(
            "purdue_af_mcp_tool_calls_total",
            {"tool": tool, "outcome": outcome, "username": username},
        )
        or 0.0
    )


def _tool_duration_count(tool: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "purdue_af_mcp_tool_duration_seconds_count", {"tool": tool}
        )
        or 0.0
    )


def test_tool_outcome_classifier():
    assert metrics.tool_outcome("ok") == "success"
    assert metrics.tool_outcome({"not": "a string"}) == "success"
    # User mistakes / nothing running
    assert (
        metrics.tool_outcome("Error: no running server found — start a pod first.")
        == "user_error"
    )
    assert metrics.tool_outcome("Error: n_workers must be ≥ 0.") == "user_error"
    assert metrics.tool_outcome("Unknown gateway 'nope'.") == "user_error"
    # Backend failures
    assert (
        metrics.tool_outcome("Error: JupyterHub API unreachable — boom")
        == "upstream_error"
    )
    assert (
        metrics.tool_outcome("Error: Loki connection failed — refused")
        == "upstream_error"
    )
    assert metrics.tool_outcome("Error: HTTP 502 — bad gateway") == "upstream_error"
    assert (
        metrics.tool_outcome("Error: Loki returned HTTP 500 — oops") == "upstream_error"
    )
    assert (
        metrics.tool_outcome(
            "Could not read profile list — service may be misconfigured."
        )
        == "upstream_error"
    )


async def test_instrumented_tool_records_success_and_duration():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-metrics")
    metrics.instrument_mcp(mcp)

    @mcp.tool()
    async def hello() -> str:
        return "hi"

    before = _tool_counter_value("hello", "success")
    duration_before = _tool_duration_count("hello")

    assert await mcp._tool_manager.call_tool("hello", {}) == "hi"
    assert _tool_counter_value("hello", "success") == before + 1
    assert _tool_duration_count("hello") == duration_before + 1


async def test_instrumented_tool_records_username_from_context():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-metrics")
    metrics.instrument_mcp(mcp)

    @mcp.tool()
    async def whoami() -> str:
        return "you"

    before = _tool_counter_value("whoami", "success", username="alice")
    ctx_token = current_user.set({"username": "alice"})
    try:
        await mcp._tool_manager.call_tool("whoami", {})
    finally:
        current_user.reset(ctx_token)

    assert _tool_counter_value("whoami", "success", username="alice") == before + 1


async def test_instrumented_tool_records_string_error():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-metrics")
    metrics.instrument_mcp(mcp)

    @mcp.tool()
    async def fail_soft() -> str:
        return "Error: something went wrong"

    before = _tool_counter_value("fail_soft", "user_error")

    out = await mcp._tool_manager.call_tool("fail_soft", {})

    assert out.startswith("Error:")
    assert _tool_counter_value("fail_soft", "user_error") == before + 1


async def test_instrumented_tool_records_exception():
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    mcp = FastMCP("test-metrics")
    metrics.instrument_mcp(mcp)

    @mcp.tool()
    async def fail_hard() -> str:
        raise RuntimeError("boom")

    before = _tool_counter_value("fail_hard", "exception")

    with pytest.raises(ToolError):
        await mcp._tool_manager.call_tool("fail_hard", {})

    assert _tool_counter_value("fail_hard", "exception") == before + 1


# ── upstream (outbound) request metrics ───────────────────────────────────────


def _upstream_counter_value(target: str, outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "purdue_af_mcp_upstream_requests_total",
            {"target": target, "outcome": outcome},
        )
        or 0.0
    )


@respx.mock
async def test_instrumented_transport_records_success():
    respx.get("http://backend/ok").respond(200)
    before = _upstream_counter_value("t-ok", "success")

    async with httpx.AsyncClient(
        transport=metrics.instrumented_transport("t-ok")
    ) as client:
        await client.get("http://backend/ok")

    assert _upstream_counter_value("t-ok", "success") == before + 1


@respx.mock
async def test_instrumented_transport_records_http_error():
    respx.get("http://backend/boom").respond(503)
    before = _upstream_counter_value("t-boom", "http_error")

    async with httpx.AsyncClient(
        transport=metrics.instrumented_transport("t-boom")
    ) as client:
        await client.get("http://backend/boom")

    assert _upstream_counter_value("t-boom", "http_error") == before + 1


@respx.mock
async def test_instrumented_transport_records_connection_error():
    respx.get("http://backend/down").mock(side_effect=httpx.ConnectError("down"))
    before = _upstream_counter_value("t-down", "connection_error")

    async with httpx.AsyncClient(
        transport=metrics.instrumented_transport("t-down")
    ) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("http://backend/down")

    assert _upstream_counter_value("t-down", "connection_error") == before + 1


@respx.mock
async def test_instrumented_transport_callable_target():
    respx.get("http://a/x").respond(200)
    before = _upstream_counter_value("host-a", "success")

    async with httpx.AsyncClient(
        transport=metrics.instrumented_transport(
            lambda request: f"host-{request.url.host}"
        )
    ) as client:
        await client.get("http://a/x")

    assert _upstream_counter_value("host-a", "success") == before + 1
