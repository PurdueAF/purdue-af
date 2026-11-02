"""Fixtures for the agentic-interface suite (helpers in agentic_helpers.py)."""

import asyncio
import os

import pytest
from common import REPO

# The image bundles the skill beside server.py.
os.environ.setdefault(
    "SKILL_PATH",
    str(REPO / ".claude" / "skills" / "purdue-af-agentic-interface" / "SKILL.md"),
)

import auth  # noqa: E402
import server  # noqa: E402
from agentic_helpers import USER  # noqa: E402
from context import current_user  # noqa: E402


@pytest.fixture(autouse=True)
def clean_user_cache():
    """Each test starts with empty token caches (positive and negative)."""
    auth._user_cache.clear()
    auth._negative_cache.clear()
    yield
    auth._user_cache.clear()
    auth._negative_cache.clear()


@pytest.fixture
def user_ctx():
    """Bind a standard authenticated user for the duration of a test."""
    ctx_token = current_user.set(dict(USER))
    yield dict(USER)
    current_user.reset(ctx_token)


@pytest.fixture
def no_sleep(monkeypatch):
    """asyncio.sleep returns at once. Opt-in: _fake_clock records its own sleeps."""

    async def sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", sleep)


@pytest.fixture
def accept_token(monkeypatch):
    """server.resolve_user takes "good" as alice; returns the user that binds."""
    alice = {"username": "alice", "token": "good"}

    async def resolve(token):
        return alice if token == "good" else None

    monkeypatch.setattr(server, "resolve_user", resolve)
    return alice
