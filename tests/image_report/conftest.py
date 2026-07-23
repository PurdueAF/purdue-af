"""Fixtures for the image build-report analyzer tests."""

import pytest
from common import REPO, load_script


@pytest.fixture(scope="session")
def analyze():
    return load_script(
        REPO / "docker" / "analyze_image_build.py",
        "analyze_image_build",
    )
