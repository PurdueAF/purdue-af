"""Tests for .github/workflows/bump-agentic-version.py (the deployment.yaml
rewriter used by ci.yml's auto-release publish step). Runs against the REAL
deployment.yaml text, so if the manifest's layout drifts away from what the
release automation expects, this suite fails before a release does."""

import re

import pytest
from common import REPO, load_script

BUMP_PATH = REPO / ".github" / "workflows" / "bump-agentic-version.py"
DEPLOYMENT_PATH = REPO / "apps" / "agentic-interface" / "deployment.yaml"


@pytest.fixture(scope="session")
def bump():
    return load_script(BUMP_PATH, "bump_agentic_version")


@pytest.fixture()
def deployment_text():
    return DEPLOYMENT_PATH.read_text()


def test_current_version_is_readable(bump, deployment_text):
    cur = bump.current_version(deployment_text)
    # `latest` (pre-first-release) reads as the 0.0.0 baseline.
    assert re.fullmatch(r"\d+\.\d+\.\d+", cur)


def test_current_version_missing_exits(bump):
    with pytest.raises(SystemExit, match="cannot find the agentic-interface"):
        bump.current_version("no image line here\n")


def test_latest_reads_as_zero_baseline(bump):
    text = "          image: reg.example/purdueaf/agentic-interface:latest\n"
    assert bump.current_version(text) == "0.0.0"


def test_bump_arithmetic(bump):
    assert bump.bump_version("0.0.0", "patch") == "0.0.1"
    assert bump.bump_version("0.3.5", "minor") == "0.4.0"
    assert bump.bump_version("0.3.5", "major") == "1.0.0"


def test_apply_rewrites_the_image_line_in_real_deployment(bump, deployment_text):
    new = bump.apply(deployment_text, "9.9.9")
    assert "/agentic-interface:9.9.9" in new
    assert "/agentic-interface:latest" not in new
    # a released version bumps again cleanly
    assert "/agentic-interface:8.8.8" in bump.apply(new, "8.8.8")


def test_apply_refuses_ambiguous_layout(bump, deployment_text):
    doubled = deployment_text + deployment_text
    with pytest.raises(SystemExit, match="expected exactly 1"):
        bump.apply(doubled, "9.9.9")
