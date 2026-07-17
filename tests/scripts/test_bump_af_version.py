"""Tests for .github/workflows/bump-af-version.py (the Release AF workflow's
values.yaml rewriter). Runs against the REAL values.yaml text, so if the
file's layout drifts away from what the release automation expects, this
suite fails before a release does."""

import re

import pytest
from common import REPO, load_script


@pytest.fixture(scope="session")
def bump():
    return load_script(
        REPO / ".github" / "workflows" / "bump-af-version.py", "bump_af_version"
    )


@pytest.fixture()
def values_text():
    return (REPO / "apps" / "jupyterhub" / "jupyterhub" / "values.yaml").read_text()


def test_current_version_is_readable(bump, values_text):
    cur = bump.current_version(values_text)
    assert re.fullmatch(r"\d+\.\d+\.\d+", cur)


def test_bump_arithmetic(bump):
    assert bump.bump_version("0.12.5", "patch") == "0.12.6"
    assert bump.bump_version("0.12.5", "minor") == "0.13.0"
    assert bump.bump_version("0.12.5", "major") == "1.0.0"


def test_apply_rewrites_all_five_spots_in_real_values(bump, values_text):
    registry = bump.DEFAULT_REGISTRY
    new = bump.apply(values_text, "9.9.9", registry)

    cur = bump.current_version(values_text)
    assert cur not in new, f"old version {cur} still present somewhere"
    assert 'tag: "9.9.9"' in new
    assert 'docker_image_tag: "9.9.9"' in new
    assert 'display_name: "Purdue AF 9.9.9' in new
    assert f'image: "{registry}:9.9.9"' in new
    assert f'name: "{registry}"' in new


def test_apply_leaves_other_images_alone(bump, values_text):
    new = bump.apply(values_text, "9.9.9", bump.DEFAULT_REGISTRY)
    # the pre-release profile (moving tag, not semver) must be untouched
    assert new.count("purdue-af:pre-release") == values_text.count(
        "purdue-af:pre-release"
    )
    # the minimal-profile image must be untouched
    assert "cmsaf-base-notebook:1.1" in new


def test_apply_is_idempotent_shape(bump, values_text):
    """A second release on the rewritten file must find the same 5 spots."""
    once = bump.apply(values_text, "9.9.9", bump.DEFAULT_REGISTRY)
    twice = bump.apply(once, "10.0.0", bump.DEFAULT_REGISTRY)
    assert "9.9.9" not in twice
    assert 'docker_image_tag: "10.0.0"' in twice


def test_apply_fails_loudly_on_layout_drift(bump):
    with pytest.raises(SystemExit):
        bump.apply("nothing that matches here\n", "9.9.9", bump.DEFAULT_REGISTRY)
