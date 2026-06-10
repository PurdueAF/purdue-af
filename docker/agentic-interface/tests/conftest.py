"""Shared fixtures for the agentic-interface test suite."""

import sys
from pathlib import Path

import pytest

# The service is a flat application (server.py, auth.py, tools/), not an
# installed package — make its directory importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
from context import current_user  # noqa: E402


@pytest.fixture(autouse=True)
def clean_user_cache():
    """Each test starts with an empty token cache."""
    auth._user_cache.clear()
    yield
    auth._user_cache.clear()


class ToolRecorder:
    """Stand-in for FastMCP that records registered tools and prompts."""

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


def register_tools(module):
    """Run a tool module's register() and return its captured tools/prompts."""
    recorder = ToolRecorder()
    module.register(recorder)
    return recorder


USER = {
    "username": "alice",
    "pod_name": "purdue-af-alice",
    "namespace": "cms",
    "token": "tok-alice",
}


@pytest.fixture
def user_ctx():
    """Bind a standard authenticated user for the duration of a test."""
    ctx_token = current_user.set(dict(USER))
    yield dict(USER)
    current_user.reset(ctx_token)


@pytest.fixture
def podless_user_ctx():
    """A user with no running server (pod_name empty)."""
    user = {**USER, "pod_name": ""}
    ctx_token = current_user.set(user)
    yield user
    current_user.reset(ctx_token)
