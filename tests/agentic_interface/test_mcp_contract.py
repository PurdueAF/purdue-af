"""MCP contract tests — exercise the *real* MCPServer, not test fakes.

The rest of the suite registers tools against a recorder, which would stay
green even if the SDK failed to generate schemas for a tool (the exact risk
of an `mcp` dependency bump). These tests are the canary for that.
"""

import json
from contextlib import asynccontextmanager

import httpx
import httpx2
import respx
import server
from agentic_helpers import STABLE_PROFILE, counter, fake_profiles
from asgi_lifespan import LifespanManager
from common import REPO, load_script
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ElicitResult
from tools import dask, session

# What each tool says it does, and "facility" where it answers for no one person.
EXPECTED_TOOLS = {
    # health
    "get_facility_health": "reads facility",
    "get_facility_metrics": "reads facility",
    # prometheus
    "query_prometheus": "reads facility",
    "list_prometheus_metrics": "reads facility",
    # logs
    "query_notebook_logs": "reads",
    "query_dask_logs": "reads",
    # storage
    "query_storage_usage": "reads",
    # dask
    "list_dask_clusters": "reads",
    "list_dask_cluster_options": "reads facility",
    "create_dask_cluster": "changes",
    "get_dask_cluster_info": "reads",
    "get_dask_worker_count": "reads",
    "get_dask_cluster_usage": "reads",
    "scale_dask_cluster": "changes",
    "stop_dask_cluster": "destroys",
    # ray
    "get_ray_capacity": "reads facility",
    "submit_ray_job": "changes",
    "list_ray_jobs": "reads",
    "get_ray_job": "reads",
    "stop_ray_job": "destroys",
    "delete_ray_job": "destroys",
    "get_ray_cluster": "reads",
    "start_ray_cluster": "changes",
    "stop_ray_cluster": "destroys",
    # profiles + session lifecycle
    "list_af_profiles": "reads facility",
    "get_session_status": "reads",
    "start_af_session": "changes",
    "stop_af_session": "destroys",
    "wait_for_session": "reads",
    "restart_af_session": "destroys",
}

EXPECTED_PROMPTS = {"create_cluster"}


async def test_all_tools_registered_with_schemas():
    tools = await server.mcp.list_tools()

    assert {t.name for t in tools} == set(EXPECTED_TOOLS)
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.input_schema.get("type") == "object", (
            f"{tool.name} has no generated input schema"
        )


def said(tool):
    hints, meta = tool.annotations, tool.meta or {}
    if hints is None or hints.read_only_hint is None:
        return "nothing"
    does = (
        "reads"
        if hints.read_only_hint
        else "destroys"
        if hints.destructive_hint
        else "changes"
    )
    return f"{does} facility" if meta.get("purdue-af/scope") == "facility" else does


async def test_every_tool_says_what_it_does_and_for_whom():
    tools = await server.mcp.list_tools()
    assert {t.name: said(t) for t in tools} == EXPECTED_TOOLS


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
MCP_ENDPOINT = f"http://hub{MCP_URL}"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


async def test_full_stack_handshake_and_auth(monkeypatch, accept_token):
    """One lifespan (the session manager is single-run), both auth outcomes."""
    app = server.build_app()
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hub") as c:
            ok = await c.post(
                MCP_URL,
                json=initialize_payload(),
                headers={**MCP_HEADERS, "Authorization": "Bearer good"},
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
            fake_profiles(monkeypatch, STABLE_PROFILE)
            success = dict(tool="list_af_profiles", outcome="success", username="alice")
            before = counter("purdue_af_mcp_tool_calls_total", **success)
            tool_resp = await c.post(
                MCP_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "list_af_profiles", "arguments": {}},
                },
                headers={**MCP_HEADERS, "Authorization": "Bearer good"},
            )
            assert tool_resp.status_code == 200
            assert counter("purdue_af_mcp_tool_calls_total", **success) == before + 1


# ── a declined elicitation must still be readable by the agent ────────────────
# The metrics._needs_input_result docstring states the output-schema constraint.


async def test_every_tool_declares_a_wrapped_string_output_schema():
    """metrics._needs_input_result hard-codes {"result": <string>}."""
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


async def test_modern_client_that_takes_no_forms_gets_help_text(caplog):
    """A client on protocol 2026-07-28 that declares no elicitation is never
    sent a question: it gets the same ask-in-chat instructions."""
    with caplog.at_level("INFO", logger="tools.elicitation"):
        async with Client(server.mcp) as client:
            result = await client.call_tool("create_dask_cluster", {})

    assert "the client takes no forms" in caplog.text
    assert "protocol=2026-07-28" in caplog.text
    _assert_help_text(result)


# ── both protocol eras over HTTP, through the assembled stack ─────────────────


@asynccontextmanager
async def served():
    """The app behind an HTTP client that signs in as alice."""
    app = server.build_app()
    async with LifespanManager(app) as manager:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=manager.app),
            headers={"Authorization": "Bearer good"},
        ) as http:
            yield http


def answering(answers, asked):
    async def answer(context, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content=answers[params.message])

    return answer


@respx.mock
async def test_modern_client_answers_each_form_and_gets_its_cluster(accept_token):
    """Protocol 2026-07-28 over stateless HTTP: each question is the tool's
    result, and the client calls again with the answer and the sealed state."""
    create = respx.post(f"{dask.DASK_GATEWAY_URL}/api/v1/clusters/").respond(
        201, json={"name": "cms.g"}
    )
    answers = {
        "Choose the worker environment.": {"env_source": "pixi"},
        "Provide the path to your pixi project.": {
            "pixi_project": "/work/users/alice/proj",
            "pixi_env": "ml",
        },
        "Choose the worker size.": {"size": "default"},
        "How many workers should the cluster start with?": {"count": "0"},
    }
    asked = []

    async with served() as http:
        transport = streamable_http_client(MCP_ENDPOINT, http_client=http)
        async with Client(
            transport, elicitation_callback=answering(answers, asked)
        ) as client:
            first = await client.session.call_tool(
                "create_dask_cluster", {}, allow_input_required=True
            )
            result = await client.call_tool("create_dask_cluster", {})

    assert not first.request_state.startswith("{")
    assert asked == list(answers)
    assert "Cluster 'cms.g' created." in result.content[0].text
    options = json.loads(create.calls.last.request.content)["cluster_options"]
    assert options["pixi_project"] == "/work/users/alice/proj"
    assert options["pixi_env"] == "ml"
    assert create.call_count == 1


@respx.mock
async def test_modern_client_chooses_a_profile_and_its_options(
    monkeypatch, accept_token
):
    hub = respx.post(f"{session.HUB_API_URL}/users/alice/server").respond(201)
    interface = {
        "display_name": "Interface",
        "choices": {"1": "JupyterLab", "2": "VS Code"},
        "default": "1",
    }
    fake_profiles(
        monkeypatch,
        {**STABLE_PROFILE, "options": {"3-interface": interface}},
        {**STABLE_PROFILE, "slug": "pre-release", "default": False},
    )
    answers = {
        "Choose a session profile.": {"profile": "stable"},
        "Interface:": {"value": "2"},
    }
    asked = []

    async with served() as http:
        transport = streamable_http_client(MCP_ENDPOINT, http_client=http)
        async with Client(
            transport, elicitation_callback=answering(answers, asked)
        ) as client:
            result = await client.call_tool("start_af_session", {})

    assert asked == list(answers)
    assert "Session is starting" in result.content[0].text
    assert json.loads(hub.calls.last.request.content) == {
        "profile": "stable",
        "3-interface": "2",
    }


async def test_handshake_client_lists_and_calls_tools(monkeypatch, accept_token):
    """PurdueAF Agent's own client code: the initialize handshake, with no
    session to keep, and the ask-in-chat instructions in place of a form."""
    bot = load_script(REPO / "apps/mattermost-agent/agent.py", "bot_as_mcp_client")
    fake_profiles(monkeypatch, STABLE_PROFILE)

    async with served() as http:

        @asynccontextmanager
        async def signed_in(headers):
            assert headers == {"Authorization": "Bearer good"}
            yield http

        monkeypatch.setattr(bot, "create_mcp_http_client", signed_in)
        facility = bot.Facility(MCP_ENDPOINT, token_file=None)
        async with facility.session("good") as mcp_session:
            assert mcp_session.protocol_version < "2026-07-28"
            listed = await mcp_session.list_tools()
            profiles = await mcp_session.call_tool("list_af_profiles", {})
            choices = await mcp_session.call_tool("create_dask_cluster", {})

    assert {t.name for t in listed.tools} == set(EXPECTED_TOOLS)
    assert "purdue-af-agentic-interface skill" in facility.instructions
    assert "Stable" in profiles.content[0].text
    _assert_help_text(choices)
