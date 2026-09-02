"""Fixtures for the agentic-interface suite (helpers in agentic_helpers.py)."""

import sys
from pathlib import Path

import pytest

# The service is a flat application (server.py, auth.py, tools/), not an
# installed package — make its directory importable before anything else.
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "docker" / "agentic-interface"))
# gpu_queries.py is shared with the hub config and copied into the image.
sys.path.insert(1, str(_REPO / "apps" / "jupyterhub" / "jupyterhub" / "extraFiles"))

import auth  # noqa: E402
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
