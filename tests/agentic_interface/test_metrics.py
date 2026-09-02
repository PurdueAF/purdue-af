"""Tests for Prometheus metrics (metrics.py + /metrics endpoint)."""

import logging

import httpx
import metrics
import pytest
import respx
import server
from context import current_user
from mcp.server.auth.provider import AccessToken
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

    await server._AuthMiddleware((lambda *a: None))(
        http_scope("/metrics"), noop_receive, send
    )

    assert send.status == 200
    assert b"purdue_af_mcp_api_calls_total" in send.body
    assert b"purdue_af_mcp_tool_calls_total" in send.body
    assert _counter_value("metrics", "200") == before + 1


async def test_mcp_request_increments_api_call_counter(monkeypatch):
    async def accept(token):
        return AccessToken(token="t", client_id="alice", scopes=[])

    monkeypatch.setattr(server, "verify_token", accept)

    class Inner:
        calls = 0

        async def __call__(self, scope, receive, send):
            Inner.calls += 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    inner = Inner()
    app = server._AuthMiddleware(inner)
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

    await server._AuthMiddleware((lambda *a: None))(
        http_scope(f"{server.SERVICE_PREFIX}/mcp"), noop_receive, send
    )

    assert send.status == 401
    assert _counter_value("mcp", "401") == before + 1


def test_record_request_increments_counter():
    before = _counter_value("other", "418")
    metrics.record_request("other", 418)
    assert _counter_value("other", "418") == before + 1


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


def _instrumented():
    mcp = metrics.InstrumentedFastMCP("test-metrics-instrumented")
    return mcp


async def test_failure_subclass_sets_the_outcome_label():
    """Tools raise errors.Failure subclasses; the subclass is the outcome."""
    import errors

    mcp = _instrumented()

    @mcp.tool()
    async def refuse(kind: str) -> str:
        raise {
            "user": errors.UserError,
            "auth": errors.AuthError,
            "upstream": errors.UpstreamError,
            "fault": errors.ServiceFault,
        }[kind]("Error: nope.")

    for kind, outcome in (
        ("user", "user_error"),
        ("auth", "auth_error"),
        ("upstream", "upstream_error"),
        ("fault", "exception"),
    ):
        before = _tool_counter_value("refuse", outcome)
        with pytest.raises(errors.Failure) as info:
            await mcp.call_tool("refuse", {"kind": kind})
        # the message reaches the client unwrapped — no "Error executing tool"
        assert str(info.value) == "Error: nope."
        assert _tool_counter_value("refuse", outcome) == before + 1


async def test_instrumented_tool_records_success_and_duration():
    mcp = metrics.InstrumentedFastMCP("test-metrics")

    @mcp.tool()
    async def hello() -> str:
        return "hi"

    before = _tool_counter_value("hello", "success")
    duration_before = _tool_duration_count("hello")

    content, _structured = await mcp.call_tool("hello", {})
    assert content[0].text == "hi"
    assert _tool_counter_value("hello", "success") == before + 1
    assert _tool_duration_count("hello") == duration_before + 1


async def test_instrumented_tool_records_username_from_context():
    mcp = metrics.InstrumentedFastMCP("test-metrics")

    @mcp.tool()
    async def whoami() -> str:
        return "you"

    before = _tool_counter_value("whoami", "success", username="alice")
    ctx_token = current_user.set({"username": "alice"})
    try:
        await mcp.call_tool("whoami", {})
    finally:
        current_user.reset(ctx_token)

    assert _tool_counter_value("whoami", "success", username="alice") == before + 1


async def test_instrumented_tool_records_exception():
    from mcp.server.fastmcp.exceptions import ToolError

    mcp = metrics.InstrumentedFastMCP("test-metrics")

    @mcp.tool()
    async def fail_hard() -> str:
        raise RuntimeError("boom")

    before = _tool_counter_value("fail_hard", "exception")

    with pytest.raises(ToolError):
        await mcp.call_tool("fail_hard", {})

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


# ── exception safety net ──────────────────────────────────────────────────────
#
# A tool that raises must still leave the user with a message that says whose
# fault it is, and must leave operators a traceback in the log.


async def test_unhandled_exception_becomes_a_clear_error_and_is_logged(caplog):
    from mcp.server.fastmcp.exceptions import ToolError

    mcp = _instrumented()

    @mcp.tool()
    async def boom() -> str:
        raise KeyError("name")

    before = _tool_counter_value("boom", "exception")
    with caplog.at_level(logging.ERROR, logger="agentic.tools"):
        with pytest.raises(ToolError) as info:
            await mcp.call_tool("boom", {})

    message = str(info.value)
    assert message.startswith("Error: boom failed unexpectedly (KeyError: 'name')")
    assert "AF support" in message
    assert "Error executing tool" not in message
    assert _tool_counter_value("boom", "exception") == before + 1
    record = next(r for r in caplog.records if "tool=boom" in r.getMessage())
    assert record.exc_info is not None  # traceback attached for operators


async def test_invalid_arguments_are_blamed_on_the_call():
    from mcp.server.fastmcp.exceptions import ToolError

    mcp = _instrumented()

    @mcp.tool()
    async def add(n: int) -> str:
        return str(n)

    with pytest.raises(ToolError) as info:
        await mcp.call_tool("add", {"n": "many"})

    message = str(info.value)
    assert message.startswith("Error: add was called with invalid arguments")
    assert "argument names and types" in message
    assert "failed unexpectedly" not in message


async def test_unknown_tool_error_passes_through():
    from mcp.server.fastmcp.exceptions import ToolError

    mcp = _instrumented()
    with pytest.raises(ToolError, match="Unknown tool"):
        await mcp.call_tool("nope", {})


async def test_url_elicitation_error_is_not_rewritten():
    """The SDK turns this one into a protocol error (-32042); leave it alone."""
    from mcp.shared.exceptions import UrlElicitationRequiredError
    from mcp.types import ElicitRequestURLParams

    mcp = _instrumented()
    needed = UrlElicitationRequiredError(
        [
            ElicitRequestURLParams(
                message="authorise", url="https://example.org", elicitationId="e1"
            )
        ]
    )

    @mcp.tool()
    async def needs_url() -> str:
        raise needed

    with pytest.raises(UrlElicitationRequiredError) as info:
        await mcp.call_tool("needs_url", {})
    assert info.value is needed
