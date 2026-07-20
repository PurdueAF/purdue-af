"""Tests for .github/workflows/bump-aux-image.py (release-image.yml's
manifest rewriter for aux images), run against the REAL manifests under
apps/ so layout drift fails here before it fails a release."""

import re
import shutil

import pytest
from common import REPO, load_script


@pytest.fixture(scope="session")
def bump():
    return load_script(
        REPO / ".github" / "workflows" / "bump-aux-image.py", "bump_aux_image"
    )


def test_versioned_image_is_discovered(bump):
    versions, files = bump.find_refs("af-node-monitor", REPO / "apps")
    assert files, "no manifests reference af-node-monitor"
    cur = bump.resolve_current(versions, "af-node-monitor")
    assert re.fullmatch(r"\d+\.\d+\.\d+", cur)


@pytest.mark.parametrize("name", ["agentic-interface", "af-pod-monitor"])
def test_continuous_channel_images_are_refused(bump, name):
    """:latest-channel images must never be manually versioned."""
    versions, _ = bump.find_refs(name, REPO / "apps")
    assert versions, f"no manifests reference {name}"
    with pytest.raises(SystemExit) as err:
        bump.resolve_current(versions, name)
    assert "continuous" in str(err.value)


def test_apply_rewrites_to_proxy_path(bump, tmp_path):
    apps = tmp_path / "apps"
    src = REPO / "apps" / "monitoring" / "af-monitoring"
    shutil.copytree(src, apps / "monitoring" / "af-monitoring")

    n = bump.apply("af-node-monitor", "9.9.9", apps_dir=apps)
    assert n >= 1
    rewritten = (
        apps / "monitoring" / "af-monitoring" / "deployment-af-node-monitor.yaml"
    ).read_text()
    assert f"{bump.PROXY}/af-node-monitor:9.9.9" in rewritten


def test_apply_refuses_when_nothing_matches(bump, tmp_path):
    (tmp_path / "apps").mkdir()
    with pytest.raises(SystemExit):
        bump.apply("af-node-monitor", "9.9.9", apps_dir=tmp_path / "apps")


def test_unknown_image_fails(bump):
    versions, _ = bump.find_refs("no-such-image", REPO / "apps")
    with pytest.raises(SystemExit):
        bump.resolve_current(versions, "no-such-image")
