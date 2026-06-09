"""Shared fixtures for the agentic-interface test suite."""

import sys
from pathlib import Path

import pytest

# The service is a flat application (server.py, auth.py, tools/), not an
# installed package — make its directory importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402


@pytest.fixture(autouse=True)
def clean_user_cache():
    """Each test starts with an empty token cache."""
    auth._user_cache.clear()
    yield
    auth._user_cache.clear()
