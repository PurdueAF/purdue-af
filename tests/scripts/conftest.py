"""Fixtures for standalone-script tests (helpers in script_helpers.py)."""

import os

import pytest
from common import REPO, load_script

# Headless backend for matplotlib (the cron host has no display either).
os.environ.setdefault("MPLBACKEND", "Agg")


@pytest.fixture(scope="session")
def plot_af_users():
    return load_script(
        REPO / "apps" / "af-utils" / "af-users-graph" / "plot-af-users.py",
        "plot_af_users",
    )
