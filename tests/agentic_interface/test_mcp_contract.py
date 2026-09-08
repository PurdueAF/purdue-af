"""MCP contract tests — exercise the *real* FastMCP server, not test fakes.

The rest of the suite registers tools against a recorder, which would stay
green even if FastMCP failed to generate schemas for a tool (the exact risk
of an `mcp` dependency bump). These tests are the canary for that.
"""

import json
from pathlib import Path

import httpx
import server
from asgi_lifespan import LifespanManager

EXPECTED_TOOLS = {
    # health
    "get_facility_health",
    # logs
    "query_notebook_logs",
    "query_dask_logs",
    # storage
    "query_storage_usage",
    # dask
    "list_dask_clusters",
    "list_dask_cluster_options",
    "create_dask_cluster",
    "get_dask_cluster_info",
    "get_dask_worker_count",
    "get_dask_cluster_usage",
    "scale_dask_cluster",
    "stop_dask_cluster",
    # profiles + session lifecycle
    "list_af_profiles",
    "get_session_status",
    "start_af_session",
    "stop_af_session",
    "wait_for_session",
    "restart_af_session",
}

EXPECTED_PROMPTS = {"create_cluster"}


async def test_all_tools_registered_with_schemas():
    tools = await server.mcp.list_tools()

    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.inputSchema.get("type") == "object", (
            f"{tool.name} has no generated input schema"
        )


async def test_all_prompts_registered():
    prompts = await server.mcp.list_prompts()
    assert {p.name for p in prompts} == EXPECTED_PROMPTS


async def test_tool_arguments_survive_schema_generation():
    """Spot-check that typed/optional parameters made it into the schema."""
    tools = {t.name: t for t in await server.mcp.list_tools()}

    logs_props = tools["query_notebook_logs"].inputSchema["properties"]
    assert {"start", "end", "limit", "direction", "filter", "dedup"} <= set(logs_props)

    scale = tools["scale_dask_cluster"].inputSchema
    assert "n_workers" in scale["properties"]
    assert "cluster_name" in scale["required"]


# ── end-to-end through the assembled ASGI stack ───────────────────────────────


def initialize_payload():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "contract-test", "version": "0"},
        },
    }


MCP_URL = f"{server.SERVICE_PREFIX}/mcp"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}

# The labels the tools/call below is expected to land on. This suite runs the
# app in its default stateless mode, where no Mcp-Session-Id ties a tools/call
# back to the initialize that named the client — so identification falls back
# to the User-Agent, and httpx's is classified as "script". The deployment runs
# stateful (MCP_STATELESS_HTTP=false), which test_clients.py covers instead.
# Host is "hub", which is not an in-cluster address, hence origin "external".
TOOL_LABELS = {
    "tool": "list_af_profiles",
    "outcome": "success",
    "username": "alice",
    "client": "script",
    "origin": "external",
}


async def test_full_stack_handshake_and_auth(monkeypatch):
    """One lifespan (the session manager is single-run), both auth outcomes."""

    from mcp.server.auth.provider import AccessToken

    async def accept(token):
        if token != "good-token":
            return None
        return AccessToken(token=token, client_id="alice", scopes=[])

    monkeypatch.setattr(server, "verify_token", accept)

    app = server._AuthMiddleware(server.mcp.streamable_http_app())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hub") as c:
            ok = await c.post(
                MCP_URL,
                json=initialize_payload(),
                headers={**MCP_HEADERS, "Authorization": "Bearer good-token"},
            )
            assert ok.status_code == 200
            assert "purdue-af-agentic-interface" in ok.text  # serverInfo.name

            denied = await c.post(
                MCP_URL,
                json=initialize_payload(),
                headers={**MCP_HEADERS, "Authorization": "Bearer wrong"},
            )
            assert denied.status_code == 401
            assert json.loads(denied.text)["error"] == "Invalid JupyterHub token"

            # tools/call must increment the tool counter via call_tool instrumentation.
            from prometheus_client import REGISTRY
            from tools import profiles

            async def fake_get_profiles(force=False):
                return [
                    {
                        "display_name": "Stable",
                        "slug": "stable",
                        "default": True,
                        "description": "",
                        "options": {},
                    }
                ]

            monkeypatch.setattr(profiles, "get_profiles", fake_get_profiles)

            before = (
                REGISTRY.get_sample_value(
                    "purdue_af_mcp_tool_calls_total",
                    TOOL_LABELS,
                )
                or 0
            )
            tool_resp = await c.post(
                MCP_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "list_af_profiles", "arguments": {}},
                },
                headers={**MCP_HEADERS, "Authorization": "Bearer good-token"},
            )
            assert tool_resp.status_code == 200
            after = (
                REGISTRY.get_sample_value(
                    "purdue_af_mcp_tool_calls_total",
                    TOOL_LABELS,
                )
                or 0
            )
            assert after == before + 1


# ── a declined elicitation must still be readable by the agent ────────────────
#
# Tools that ask the user for choices raise NeedsChoices when the client cannot
# (or will not) render the prompt, and metrics.InstrumentedFastMCP turns that
# into an ordinary result carrying the help text. Because every tool is `-> str`
# FastMCP declares an outputSchema for it, and the low-level server discards any
# result without structuredContent — so the help text only survives the round
# trip if it is returned in both halves. These two tests pin that down: the
# shape assumption, and the actual round trip through the real server.


async def test_every_tool_declares_a_wrapped_string_output_schema():
    """metrics._needs_input_result hard-codes {"result": <string>}.

    If a tool ever returns something other than a plain string, that helper has
    to learn the new shape or the NeedsChoices path silently regresses.
    """
    for tool in await server.mcp.list_tools():
        schema = tool.outputSchema
        assert schema is not None, f"{tool.name} declares no output schema"
        assert schema.get("type") == "object", f"{tool.name}: {schema}"
        assert list(schema.get("required", [])) == ["result"], f"{tool.name}: {schema}"
        assert schema["properties"]["result"].get("type") == "string", (
            f"{tool.name}: {schema}"
        )


async def test_declined_elicitation_returns_help_text_not_a_validation_error():
    """Regression: the NeedsChoices result used to fail output validation.

    An agent client may decline elicitation without ever showing a form. The
    tool must then hand back the instructions for asking in chat — not
    "Output validation error: outputSchema defined but no structured output
    returned".
    """
    from mcp.shared.memory import create_connected_server_and_client_session
    from mcp.types import ElicitResult

    async def decline(context, params):
        return ElicitResult(action="decline")

    async with create_connected_server_and_client_session(
        server.mcp, elicitation_callback=decline
    ) as session:
        result = await session.call_tool("create_dask_cluster", {})

    assert result.isError is False
    assert result.structuredContent is not None
    text = result.content[0].text
    assert text == result.structuredContent["result"]
    assert "create_dask_cluster needs" in text


# ── the image carries every module the service imports ────────────────────────


def test_every_module_is_copied_into_the_image():
    """The Dockerfile COPYs modules one by one, so a new file is invisible to
    the image until a line is added for it — and the failure is an
    ImportError at container start, long after the tests here have passed.
    (clients.py shipped exactly this way once.)"""
    root = Path(server.__file__).resolve().parent
    dockerfile = (root / "Dockerfile").read_text()
    modules = {
        path.name
        for path in root.glob("*.py")
        # __init__.py and friends would be packaging, not service modules;
        # there are none today, and a new one should be added deliberately.
        if not path.name.startswith("_")
    }
    missing = {
        name
        for name in modules
        if f"COPY docker/agentic-interface/{name} " not in dockerfile
    }
    assert not missing, f"not COPYed into the image: {sorted(missing)}"


# ── stateful session attribution, through the real SDK ────────────────────────


async def test_stateful_session_carries_the_client_to_later_tool_calls(monkeypatch):
    """The deployment runs stateless_http=False, where later tool calls are
    attributed from the Mcp-Session-Id alone. The SDK mints that id, so only a
    real server proves the middleware reads it from the right place."""
    import clients
    import metrics
    from mcp.server.auth.provider import AccessToken
    from prometheus_client import REGISTRY

    async def accept(token):
        return AccessToken(token=token, client_id="alice", scopes=[])

    monkeypatch.setattr(server, "verify_token", accept)
    clients.reset_sessions()

    mcp = metrics.InstrumentedFastMCP(
        "stateful-test",
        stateless_http=False,
        streamable_http_path=f"{server.SERVICE_PREFIX}/mcp",
    )

    @mcp.tool()
    async def ping_tool() -> str:
        return "pong"

    def counter():
        return (
            REGISTRY.get_sample_value(
                "purdue_af_mcp_tool_calls_total",
                {
                    "tool": "ping_tool",
                    "outcome": "success",
                    "username": "alice",
                    "client": "claude-code",
                    "origin": "in_session",
                },
            )
            or 0
        )

    app = server._AuthMiddleware(mcp.streamable_http_app())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        # An in-cluster Host, so origin is exercised end to end too.
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://agentic-interface.cms.svc.cluster.local:8888",
        ) as c:
            auth = {"Authorization": "Bearer good-token"}
            init = await c.post(
                MCP_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "claude-code", "version": "2.1.263"},
                    },
                },
                headers={**MCP_HEADERS, **auth},
            )
            assert init.status_code == 200
            session_id = init.headers.get("mcp-session-id")
            assert session_id, "the SDK minted no session id for a stateful server"
            remembered = clients.lookup(session_id)
            assert remembered is not None and remembered.name == "claude-code"

            sess = {**MCP_HEADERS, **auth, "mcp-session-id": session_id}
            await c.post(
                MCP_URL,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=sess,
            )

            before = counter()
            called = await c.post(
                MCP_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "ping_tool", "arguments": {}},
                },
                headers=sess,
            )
            assert called.status_code == 200
            # The tool call carried no clientInfo; the label can only have come
            # from the session id.
            assert counter() == before + 1
