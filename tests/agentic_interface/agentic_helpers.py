"""Test helpers for the agentic-interface suite (sys.path set by conftest)."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from context import current_user
from prometheus_client import REGISTRY
from tools import profiles

USER = {
    "username": "alice",
    "token": "tok-alice",
}

STABLE_PROFILE = {
    "display_name": "Stable",
    "slug": "stable",
    "default": True,
    "description": "",
    "options": {},
}


class ToolRecorder:
    """Stand-in for MCPServer that records registered tools and prompts."""

    def __init__(self):
        self.tools = {}
        self.prompts = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def prompt(self):
        def decorator(fn):
            self.prompts[fn.__name__] = fn
            return fn

        return decorator


class FakeCtx:
    """Stand-in for the MCP SDK Context; scripts elicit() responses in order.

    A response is an (action, data) pair or an exception to raise. elicit()
    catches everything, so a question nobody scripted reads as a fallback, not
    as the IndexError it raises here.
    """

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def elicit(self, message, schema):
        self.calls.append((message, schema))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        action, data = response
        return SimpleNamespace(action=action, data=data)


async def noop_receive():  # pragma: no cover - never awaited in these tests
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


def fake_profiles(monkeypatch, *parsed):
    """Make profiles.get_profiles return `parsed`."""

    async def get_profiles():
        return list(parsed)

    monkeypatch.setattr(profiles, "get_profiles", get_profiles)


@contextmanager
def as_user(**fields):
    """Bind USER, overridden by `fields`, for the duration of the block."""
    token = current_user.set({**USER, **fields})
    try:
        yield
    finally:
        current_user.reset(token)


def counter(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def register_tools(module):
    """Run a tool module's register() and return its captured tools/prompts."""
    recorder = ToolRecorder()
    module.register(recorder)
    return recorder


async def failure(awaitable):
    """Await a tool call that must fail; return the failure's message.

    Tools raise errors.Failure (an MCP ToolError) instead of returning error
    strings, so tests that assert on a failure message go through here.
    """
    from errors import Failure

    with pytest.raises(Failure) as info:
        await awaitable
    return str(info.value)


async def needs_choices(awaitable):
    """Await a tool call that must ask for choices; return the help text.

    Tools raise NeedsChoices when a choice could not be elicited; the server
    turns it into an ordinary result, so tests that call tools directly go
    through here.
    """
    from tools.elicitation import NeedsChoices

    with pytest.raises(NeedsChoices) as info:
        await awaitable
    return str(info.value)
