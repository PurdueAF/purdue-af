"""Fixtures for the kaniko build-report tests."""

import pytest
from common import REPO, load_script


@pytest.fixture(scope="session")
def analyze():
    return load_script(
        REPO / "docker" / "kaniko-build-jobs" / "analyze_image_build.py",
        "analyze_image_build",
    )
