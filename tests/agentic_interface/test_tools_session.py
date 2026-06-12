"""Tests for tools/session.py — JupyterHub session lifecycle."""

import json

import auth
import respx
from agentic_helpers import register_tools
from tools import profiles, session

USER_URL = f"{session.HUB_API_URL}/users/alice"
SERVER_URL = f"{session.HUB_API_URL}/users/alice/server"


def server_payload(ready=True, pod="purdue-af-alice", options=None, pending=None):
    return {
        "servers": {
            "": {
                "ready": ready,
                "pending": pending,
                "started": "2026-01-01T00:00:00Z",
                "user_options": options or {},
                "state": {"pod_name": pod},
            }
        }
    }


def fake_profiles(monkeypatch):
    parsed = [
        {
            "display_name": "Stable",
            "slug": "stable",
            "default": True,
            "description": "",
            "options": {},
        }
    ]

    async def fake_get_profiles(force=False):
        return parsed

    monkeypatch.setattr(profiles, "get_profiles", fake_get_profiles)
    return parsed


# ── get_session_status ────────────────────────────────────────────────────────


@respx.mock
async def test_status_no_session(user_ctx):
    respx.get(USER_URL).respond(200, json={"servers": {}})

    tools = register_tools(session).tools
    out = await tools["get_session_status"]()

    assert "No active session" in out
    assert "/user/alice/lab" in out  # links shown even when stopped


@respx.mock
async def test_status_running_with_jupyterlab_active(user_ctx):
    respx.get(USER_URL).respond(
        200, json=server_payload(options={"0-cpu": "1", "3-interface": "1"})
    )

    tools = register_tools(session).tools
    out = await tools["get_session_status"]()

    assert "# Session status: running" in out
    assert "pod: purdue-af-alice" in out
    assert "JupyterLab" in out and "← active" in out
    assert out.index("← active") < out.index("VS Code")
    assert "0-cpu: 1" in out


@respx.mock
async def test_status_vscode_active_marker(user_ctx):
    respx.get(USER_URL).respond(200, json=server_payload(options={"3-interface": "2"}))

    tools = register_tools(session).tools
    out = await tools["get_session_status"]()

    vscode_line = next(line for line in out.splitlines() if "VS Code" in line)
    assert "← active" in vscode_line


@respx.mock
async def test_status_pending(user_ctx):
    respx.get(USER_URL).respond(200, json=server_payload(ready=False, pending="spawn"))

    tools = register_tools(session).tools
    out = await tools["get_session_status"]()
    assert "pending (spawn)" in out


@respx.mock
async def test_status_api_error(user_ctx):
    respx.get(USER_URL).respond(500)

    tools = register_tools(session).tools
    out = await tools["get_session_status"]()
    assert "HTTP 500" in out


# ── start_af_session ──────────────────────────────────────────────────────────


@respx.mock
async def test_start_session(user_ctx):
    route = respx.post(SERVER_URL).respond(201)

    tools = register_tools(session).tools
    out = await tools["start_af_session"]()

    assert "Session is starting" in out
    assert json.loads(route.calls.last.request.content) == {}


@respx.mock
async def test_start_with_profile_and_options(user_ctx, monkeypatch):
    fake_profiles(monkeypatch)
    route = respx.post(SERVER_URL).respond(201)

    tools = register_tools(session).tools
    await tools["start_af_session"](profile_name="Stable", user_options={"0-cpu": "2"})

    body = json.loads(route.calls.last.request.content)
    assert body == {"0-cpu": "2", "profile": "stable"}


async def test_start_unknown_profile(user_ctx, monkeypatch):
    fake_profiles(monkeypatch)

    tools = register_tools(session).tools
    out = await tools["start_af_session"](profile_name="ghost")

    assert "Unknown profile 'ghost'" in out
    assert '"stable"' in out  # known slugs listed


@respx.mock
async def test_start_already_running(user_ctx):
    respx.post(SERVER_URL).respond(400, text="alice's server is already running")

    tools = register_tools(session).tools
    out = await tools["start_af_session"]()
    assert "already running" in out


@respx.mock
async def test_start_rejected_options_not_masked(user_ctx):
    respx.post(SERVER_URL).respond(400, text="Invalid profile option 'x'")

    tools = register_tools(session).tools
    out = await tools["start_af_session"]()
    assert "rejected the spawn request" in out


@respx.mock
async def test_start_session_clears_token_cache(user_ctx):
    respx.post(SERVER_URL).respond(201)
    auth._user_cache["tok-alice"] = (9e9, {"username": "alice"})

    tools = register_tools(session).tools
    await tools["start_af_session"]()

    assert "tok-alice" not in auth._user_cache


# ── stop_af_session ───────────────────────────────────────────────────────────


@respx.mock
async def test_stop_session_clears_token_cache(user_ctx):
    respx.delete(SERVER_URL).respond(204)
    auth._user_cache["tok-alice"] = (9e9, {"username": "alice"})

    tools = register_tools(session).tools
    out = await tools["stop_af_session"]()

    assert "Session is stopping" in out
    assert "tok-alice" not in auth._user_cache


@respx.mock
async def test_stop_session_not_running(user_ctx):
    respx.delete(SERVER_URL).respond(400)

    tools = register_tools(session).tools
    out = await tools["stop_af_session"]()
    assert "No session is currently running" in out


# ── wait_for_session ──────────────────────────────────────────────────────────


@respx.mock
async def test_wait_returns_when_ready(user_ctx):
    respx.get(USER_URL).respond(200, json=server_payload(ready=True))
    auth._user_cache["tok-alice"] = (9e9, {"username": "alice"})

    tools = register_tools(session).tools
    out = await tools["wait_for_session"]()

    assert "Session is running" in out
    assert "pod: purdue-af-alice" in out
    assert "tok-alice" not in auth._user_cache  # cache invalidated


@respx.mock
async def test_wait_times_out(user_ctx):
    respx.get(USER_URL).respond(200, json=server_payload(ready=False))

    tools = register_tools(session).tools
    out = await tools["wait_for_session"](timeout_seconds=0)

    assert "did not become ready" in out


# ── restart_af_session ────────────────────────────────────────────────────────


@respx.mock
async def test_restart_reuses_prior_options(user_ctx, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(session.asyncio, "sleep", no_sleep)

    respx.get(USER_URL).respond(
        200, json=server_payload(options={"0-cpu": "2", "profile": "stable"})
    )
    respx.delete(SERVER_URL).respond(204)
    start_route = respx.post(SERVER_URL).respond(201)

    tools = register_tools(session).tools
    out = await tools["restart_af_session"]()

    body = json.loads(start_route.calls.last.request.content)
    assert body == {"0-cpu": "2", "profile": "stable"}
    assert "Session restarting with 0-cpu=2, profile=stable" in out


@respx.mock
async def test_restart_overrides_take_precedence(user_ctx, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(session.asyncio, "sleep", no_sleep)

    respx.get(USER_URL).respond(200, json=server_payload(options={"0-cpu": "2"}))
    respx.delete(SERVER_URL).respond(204)
    start_route = respx.post(SERVER_URL).respond(201)

    tools = register_tools(session).tools
    await tools["restart_af_session"](user_options={"0-cpu": "4"})

    assert json.loads(start_route.calls.last.request.content) == {"0-cpu": "4"}


@respx.mock
async def test_restart_clears_token_cache_after_stop(user_ctx, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(session.asyncio, "sleep", no_sleep)

    respx.get(USER_URL).respond(200, json=server_payload())
    respx.delete(SERVER_URL).respond(204)
    respx.post(SERVER_URL).respond(201)
    auth._user_cache["tok-alice"] = (9e9, {"username": "alice"})

    tools = register_tools(session).tools
    await tools["restart_af_session"]()

    assert "tok-alice" not in auth._user_cache


@respx.mock
async def test_restart_when_not_running_starts_fresh(user_ctx):
    respx.get(USER_URL).respond(200, json={"servers": {}})
    respx.delete(SERVER_URL).respond(400)  # nothing to stop
    respx.post(SERVER_URL).respond(201)

    tools = register_tools(session).tools
    out = await tools["restart_af_session"]()
    # no sleep needed (nothing was running) and restart proceeds
    assert "Session restarting" in out


@respx.mock
async def test_restart_pod_still_terminating(user_ctx, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(session.asyncio, "sleep", no_sleep)

    respx.get(USER_URL).respond(200, json=server_payload())
    respx.delete(SERVER_URL).respond(204)
    respx.post(SERVER_URL).respond(400, text="pod still terminating")

    tools = register_tools(session).tools
    out = await tools["restart_af_session"]()
    assert "still terminating" in out
    assert "start_af_session" in out  # recovery hint
