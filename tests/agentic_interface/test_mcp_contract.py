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
    # ssh
    "prepare_ssh_connection",
    "connect_to_session",
}

EXPECTED_PROMPTS = {
    "launch_session",
    "connect_session",
    "restart_session",
    "stop_session",
    "recover_ssh",
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
            "pod_name": "p",
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
