"""MCP contract tests — exercise the *real* MCPServer, not test fakes.

The rest of the suite registers tools against a recorder, which would stay
green even if the SDK failed to generate schemas for a tool (the exact risk
of an `mcp` dependency bump). These tests are the canary for that.
"""

import json

import httpx
import server
from asgi_lifespan import LifespanManager

EXPECTED_TOOLS = {
    # health
    "get_facility_health",
    "get_facility_metrics",
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
    # ray
    "get_ray_capacity",
    "submit_ray_job",
    "list_ray_jobs",
    "get_ray_job",
    "stop_ray_job",
    "delete_ray_job",
    "get_ray_cluster",
    "start_ray_cluster",
    "stop_ray_cluster",
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
        assert tool.input_schema.get("type") == "object", (
            f"{tool.name} has no generated input schema"
        )


async def test_all_prompts_registered():
    prompts = await server.mcp.list_prompts()
    assert {p.name for p in prompts} == EXPECTED_PROMPTS


async def test_tool_arguments_survive_schema_generation():
    """Spot-check that typed/optional parameters made it into the schema."""
    tools = {t.name: t for t in await server.mcp.list_tools()}

    logs_props = tools["query_notebook_logs"].input_schema["properties"]
    assert {"start", "end", "limit", "direction", "filter", "dedup"} <= set(logs_props)

    scale = tools["scale_dask_cluster"].input_schema
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


async def test_full_stack_handshake_and_auth(monkeypatch):
    """One lifespan (the session manager is single-run), both auth outcomes."""

    from mcp.server.auth.provider import AccessToken

    async def accept(token):
        if token != "good-token":
            return None
        return AccessToken(token=token, client_id="alice", scopes=[])

    monkeypatch.setattr(server, "verify_token", accept)

    app = server.build_app()
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

            async def fake_get_profiles():
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
                    {
                        "tool": "list_af_profiles",
                        "outcome": "success",
                        "username": "alice",
                    },
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
                    {
                        "tool": "list_af_profiles",
                        "outcome": "success",
                        "username": "alice",
                    },
                )
                or 0
            )
            assert after == before + 1


# ── a declined elicitation must still be readable by the agent ────────────────
#
# Tools that ask the user for choices raise NeedsChoices when the client cannot
# (or will not) render the prompt, and metrics.InstrumentedMCPServer turns that
# into an ordinary result carrying the help text. Because every tool is `-> str`
# the SDK declares an output schema for it, and clients reject a successful
# result without conforming structured content — so the help text only
# survives the round trip if it is returned in both halves. These tests pin
# that down: the shape assumption, and the round trip through the real server
# for a client on each protocol era.


async def test_every_tool_declares_a_wrapped_string_output_schema():
    """metrics._needs_input_result hard-codes {"result": <string>}.

    If a tool ever returns something other than a plain string, that helper has
    to learn the new shape or the NeedsChoices path silently regresses.
    """
    for tool in await server.mcp.list_tools():
        schema = tool.output_schema
        assert schema is not None, f"{tool.name} declares no output schema"
        assert schema.get("type") == "object", f"{tool.name}: {schema}"
        assert list(schema.get("required", [])) == ["result"], f"{tool.name}: {schema}"
        assert schema["properties"]["result"].get("type") == "string", (
            f"{tool.name}: {schema}"
        )


def _assert_help_text(result):
    assert not result.is_error
    assert result.structured_content is not None
    text = result.content[0].text
    assert text == result.structured_content["result"]
    assert "create_dask_cluster needs" in text


async def test_declined_elicitation_returns_help_text_not_a_validation_error(caplog):
    """A legacy-protocol client gets the form; declining it returns the
    ask-in-chat instructions as a normal result that passes output validation."""
    from mcp.client import Client
    from mcp.types import ElicitResult

    asked = []

    async def decline(context, params):
        asked.append(params.message)
        return ElicitResult(action="decline")

    with caplog.at_level("INFO", logger="tools.elicitation"):
        async with Client(
            server.mcp, mode="legacy", elicitation_callback=decline
        ) as client:
            result = await client.call_tool("create_dask_cluster", {})

    assert asked, "the legacy client was never shown the form"
    assert "elicit action=decline" in caplog.text
    _assert_help_text(result)


async def test_modern_client_gets_help_text_without_a_form(caplog):
    """Protocol 2026-07-28 has no server-to-client requests: the tool cannot
    push the form, and answers with the same ask-in-chat instructions."""
    from mcp.client import Client

    asked = []

    async def record(context, params):  # pragma: no cover - must not be called
        asked.append(params.message)

    with caplog.at_level("INFO", logger="tools.elicitation"):
        async with Client(server.mcp, elicitation_callback=record) as client:
            result = await client.call_tool("create_dask_cluster", {})

    assert not asked
    assert "no back-channel" in caplog.text
    assert "protocol=2026-07-28" in caplog.text
    _assert_help_text(result)
