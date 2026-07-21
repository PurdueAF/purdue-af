"""Tests for tools/session.py — JupyterHub session lifecycle."""

import json
import types

import auth
import respx
from agentic_helpers import register_tools
from tools import profiles, session

USER_URL = f"{session.HUB_API_URL}/users/alice"
SERVER_URL = f"{session.HUB_API_URL}/users/alice/server"


class _Result:
    def __init__(self, action, data=None):
        self.action = action
        self.data = data


class FakeCtx:
    """Stand-in for FastMCP Context; scripts elicit() responses in order."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def elicit(self, message, schema):
        self.calls.append((message, schema))
        if not self._responses:
            raise AssertionError(f"unexpected elicit call: {message!r}")
        action, data = self._responses.pop(0)
        return _Result(action, data)


def accept(**kwargs):
    return ("accept", types.SimpleNamespace(**kwargs))


def server_payload(ready=True, options=None, pending=None):
    return {
        "servers": {
            "": {
                "ready": ready,
                "pending": pending,
                "started": "2026-01-01T00:00:00Z",
                "user_options": options or {},
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


_STABLE_OPTIONS = {
    "3-interface": {
        "display_name": "Interface",
        "choices": {"1": "JupyterLab (default)", "2": "VS Code"},
    },
    "0-cpu": {
        "display_name": "CPU cores",
        "choices": {"1": "1 (default)", "2": "4", "3": "8"},
    },
}


def fake_profiles_with_options(monkeypatch, *, multi=False):
    parsed = [
        {
            "display_name": "Stable",
            "slug": "stable",
            "default": True,
            "description": "",
            "options": _STABLE_OPTIONS,
        }
    ]
    if multi:
        parsed.append(
            {
                "display_name": "Pre-release",
                "slug": "pre-release",
                "default": False,
                "description": "",
                "options": {},
            }
        )

    async def fake_get_profiles(force=False):
        return parsed

    monkeypatch.setattr(profiles, "get_profiles", fake_get_profiles)
    return parsed


def fake_profiles_gpu(monkeypatch):
    parsed = [
        {
            "display_name": "Stable",
            "slug": "stable",
            "default": True,
            "description": "",
            "options": {
                "1-gpu": {
                    "display_name": "GPUs",
                    "choices": {
                        "1": "0",
                        "2": "1 A100 GPU slice (5GB)",
                        "3": "1 full A100 GPU (40GB) - subject to availability",
                    },
                    "gpu": {
                        "2": "nvidia.com/mig-1g.5gb",
                        "3": "nvidia.com/mig-7g.40gb",
                    },
                }
            },
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
    assert "user: alice" in out
    assert "pod:" not in out
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
    # No profiles available (local/dev) → no elicitation, Hub default.
    out = await tools["start_af_session"](FakeCtx())

    assert "Session is starting" in out
    assert json.loads(route.calls.last.request.content) == {}


@respx.mock
async def test_start_with_profile_and_options(user_ctx, monkeypatch):
    fake_profiles(monkeypatch)
    route = respx.post(SERVER_URL).respond(201)

    tools = register_tools(session).tools
    await tools["start_af_session"](
        FakeCtx(), profile_name="Stable", user_options={"0-cpu": "2"}
    )

    body = json.loads(route.calls.last.request.content)
    assert body == {"0-cpu": "2", "profile": "stable"}


async def test_start_unknown_profile(user_ctx, monkeypatch):
    fake_profiles(monkeypatch)

    tools = register_tools(session).tools
    out = await tools["start_af_session"](FakeCtx(), profile_name="ghost")

    assert "Unknown profile 'ghost'" in out
    assert '"stable"' in out  # known slugs listed


@respx.mock
async def test_start_already_running(user_ctx):
    respx.post(SERVER_URL).respond(400, text="alice's server is already running")

    tools = register_tools(session).tools
    out = await tools["start_af_session"](FakeCtx())
    assert "already running" in out


@respx.mock
async def test_start_rejected_options_not_masked(user_ctx):
    respx.post(SERVER_URL).respond(400, text="Invalid profile option 'x'")

    tools = register_tools(session).tools
    out = await tools["start_af_session"](FakeCtx())
    assert "rejected the spawn request" in out


@respx.mock
async def test_start_session_clears_token_cache(user_ctx):
    respx.post(SERVER_URL).respond(201)
    auth._user_cache["tok-alice"] = (9e9, {"username": "alice"})

    tools = register_tools(session).tools
    await tools["start_af_session"](FakeCtx())

    assert "tok-alice" not in auth._user_cache


# ── start_af_session: elicitation flows ───────────────────────────────────────


@respx.mock
async def test_start_elicits_profile_and_options(user_ctx, monkeypatch):
    fake_profiles_with_options(monkeypatch, multi=True)
    route = respx.post(SERVER_URL).respond(201)

    ctx = FakeCtx(
        accept(profile="stable"),
        accept(value="2"),  # interface → VS Code
        accept(value="3"),  # cpu → 8
    )
    tools = register_tools(session).tools
    out = await tools["start_af_session"](ctx)

    assert "Session is starting" in out
    body = json.loads(route.calls.last.request.content)
    assert body == {"profile": "stable", "3-interface": "2", "0-cpu": "3"}
    assert len(ctx.calls) == 3


@respx.mock
async def test_start_single_profile_skips_profile_question(user_ctx, monkeypatch):
    fake_profiles_with_options(monkeypatch, multi=False)
    route = respx.post(SERVER_URL).respond(201)

    ctx = FakeCtx(
        accept(value="1"),  # interface (profile auto-selected, no profile question)
        accept(value="2"),  # cpu
    )
    tools = register_tools(session).tools
    await tools["start_af_session"](ctx)

    body = json.loads(route.calls.last.request.content)
    assert body == {"profile": "stable", "3-interface": "1", "0-cpu": "2"}
    assert len(ctx.calls) == 2


@respx.mock
async def test_start_only_elicits_missing_options(user_ctx, monkeypatch):
    fake_profiles_with_options(monkeypatch, multi=False)
    route = respx.post(SERVER_URL).respond(201)

    ctx = FakeCtx(accept(value="3"))  # only cpu asked; interface pre-supplied
    tools = register_tools(session).tools
    await tools["start_af_session"](
        ctx, profile_name="Stable", user_options={"3-interface": "2"}
    )

    body = json.loads(route.calls.last.request.content)
    assert body == {"3-interface": "2", "profile": "stable", "0-cpu": "3"}
    assert len(ctx.calls) == 1


@respx.mock
async def test_start_use_defaults_skips_elicitation(user_ctx, monkeypatch):
    fake_profiles_with_options(monkeypatch, multi=True)
    route = respx.post(SERVER_URL).respond(201)

    tools = register_tools(session).tools
    await tools["start_af_session"](FakeCtx(), use_defaults=True)

    body = json.loads(route.calls.last.request.content)
    assert body == {"profile": "stable"}


async def test_start_unsupported_client_returns_help(user_ctx, monkeypatch):
    fake_profiles_with_options(monkeypatch, multi=True)

    tools = register_tools(session).tools
    out = await tools["start_af_session"](None)
    assert "use_defaults=True" in out


async def test_start_cancelled(user_ctx, monkeypatch):
    fake_profiles_with_options(monkeypatch, multi=True)

    ctx = FakeCtx(("cancel", None))
    tools = register_tools(session).tools
    out = await tools["start_af_session"](ctx)
    assert "cancelled" in out.lower()


@respx.mock
async def test_start_gpu_question_shows_counts_and_hides_exhausted(
    user_ctx, monkeypatch
):
    fake_profiles_gpu(monkeypatch)

    async def fake_free():
        return {"nvidia.com/mig-1g.5gb": 3, "nvidia.com/mig-7g.40gb": 0}

    monkeypatch.setattr(session, "free_gpus", fake_free)
    route = respx.post(SERVER_URL).respond(201)

    ctx = FakeCtx(accept(value="2"))
    tools = register_tools(session).tools
    await tools["start_af_session"](ctx)

    prop = ctx.calls[0][1].model_json_schema()["properties"]["value"]
    assert prop["enum"] == ["1", "2"]  # exhausted 40GB flavor hidden
    assert prop["enumNames"] == ["0", "1 A100 GPU slice (5GB) — 3 available now"]

    body = json.loads(route.calls.last.request.content)
    assert body == {"profile": "stable", "1-gpu": "2"}


@respx.mock
async def test_start_gpu_question_unknown_keeps_all(user_ctx, monkeypatch):
    fake_profiles_gpu(monkeypatch)

    async def fake_free():
        return None  # Prometheus unreachable → fail open

    monkeypatch.setattr(session, "free_gpus", fake_free)
    respx.post(SERVER_URL).respond(201)

    ctx = FakeCtx(accept(value="3"))
    tools = register_tools(session).tools
    await tools["start_af_session"](ctx)

    prop = ctx.calls[0][1].model_json_schema()["properties"]["value"]
    assert prop["enum"] == ["1", "2", "3"]
    assert "subject to availability" in prop["enumNames"][2]


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
    assert "started: 2026-01-01T00:00:00Z" in out
    assert "pod:" not in out
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
