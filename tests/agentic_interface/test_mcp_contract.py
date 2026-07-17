"""MCP contract tests — exercise the *real* FastMCP server, not test fakes.

The rest of the suite registers tools against a recorder, which would stay
green even if FastMCP failed to generate schemas for a tool (the exact risk
of an `mcp` dependency bump). These tests are the canary for that.
"""

import json

import httpx
import server
from asgi_lifespan import LifespanManager

EXPECTED_TOOLS = {
    # logs
    "query_notebook_logs",
    "query_dask_logs",
    # storage
    "query_storage_usage",
    # dask
    "list_dask_clusters",
    "get_dask_cluster_info",
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

EXPECTED_PROMPTS = {
    "launch_session",
    "restart_session",
    "stop_session",
}


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


async def test_full_stack_handshake_and_auth(monkeypatch):
    """One lifespan (the session manager is single-run), both auth outcomes."""

    async def accept(token):
        if token != "good-token":
            return None
        return {
            "username": "alice",
            "namespace": "cms",
            "token": token,
        }

    monkeypatch.setattr(server, "resolve_user", accept)

    app = server._AuthMiddleware(
        server._PathStripper(server.mcp.streamable_http_app(), server.SERVICE_PREFIX)
    )
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

            # The middleware sniffs the JSON-RPC method from the POST body and
            # must replay it intact to the MCP app (asserted by the 200 above).
            assert (
                REGISTRY.get_sample_value(
                    "purdue_af_mcp_jsonrpc_requests_total",
                    {"method": "tools/call", "username": "alice"},
                )
                or 0
            ) >= 1
