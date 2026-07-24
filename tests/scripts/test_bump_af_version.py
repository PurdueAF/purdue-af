"""Tests for .github/workflows/bump-af-version.py (release-image.yml's
values.yaml rewriter). Runs against the REAL values.yaml text, so if the
file's layout drifts away from what the release automation expects, this
suite fails before a release does."""

import re
import runpy
import sys

import pytest
from common import REPO, load_script

BUMP_PATH = REPO / ".github" / "workflows" / "bump-af-version.py"
VALUES_PATH = REPO / "apps" / "jupyterhub" / "jupyterhub" / "values.yaml"


@pytest.fixture(scope="session")
def bump():
    return load_script(BUMP_PATH, "bump_af_version")


@pytest.fixture()
def values_text():
    return VALUES_PATH.read_text()


def _run_main(bump, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["bump-af-version.py", *argv])
    bump.main()


def test_current_version_is_readable(bump, values_text):
    cur = bump.current_version(values_text)
    assert re.fullmatch(r"\d+\.\d+\.\d+", cur)


def test_current_version_missing_exits(bump):
    with pytest.raises(SystemExit, match="cannot find docker_image_tag"):
        bump.current_version("no tag here\n")


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


# ── main() CLI ────────────────────────────────────────────────────────────────


def test_main_print_current(bump, tmp_path, capsys, values_text, monkeypatch):
    path = tmp_path / "values.yaml"
    path.write_text(values_text)
    cur = bump.current_version(values_text)

    _run_main(bump, monkeypatch, "--print-current", "--file", str(path))

    assert capsys.readouterr().out.strip() == cur
    assert path.read_text() == values_text  # unchanged


def test_main_bump_dry_run(bump, tmp_path, capsys, values_text, monkeypatch):
    path = tmp_path / "values.yaml"
    path.write_text(values_text)
    cur = bump.current_version(values_text)
    expected = bump.bump_version(cur, "patch")

    _run_main(bump, monkeypatch, "--bump", "patch", "--file", str(path), "--dry-run")

    out = capsys.readouterr()
    assert out.out.strip() == expected
    assert f"{cur} -> {expected}" in out.err
    assert "dry run" in out.err
    assert path.read_text() == values_text


def test_main_set_writes_file(bump, tmp_path, capsys, values_text, monkeypatch):
    path = tmp_path / "values.yaml"
    path.write_text(values_text)

    _run_main(bump, monkeypatch, "--set", "9.9.9", "--file", str(path))

    out = capsys.readouterr()
    assert out.out.strip() == "9.9.9"
    assert 'docker_image_tag: "9.9.9"' in path.read_text()


def test_main_set_rejects_bad_version(bump, tmp_path, values_text, monkeypatch):
    path = tmp_path / "values.yaml"
    path.write_text(values_text)
    with pytest.raises(SystemExit, match="expects X.Y.Z"):
        _run_main(bump, monkeypatch, "--set", "not-a-version", "--file", str(path))


def test_main_as_script(tmp_path, values_text, monkeypatch, capsys):
    """Exercise the ``if __name__ == "__main__"`` entrypoint."""
    path = tmp_path / "values.yaml"
    path.write_text(values_text)
    monkeypatch.setattr(
        sys, "argv", [str(BUMP_PATH), "--print-current", "--file", str(path)]
    )
    runpy.run_path(str(BUMP_PATH), run_name="__main__")
    assert re.fullmatch(r"\d+\.\d+\.\d+", capsys.readouterr().out.strip())
