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
SYNC_PATH = REPO / "apps" / "af-utils" / "pixi-global-sync" / "deployment.yaml"
PIXI_PATHS = [
    REPO / "pixi" / "base" / "pixi.toml",
    REPO / "pixi" / "global" / "pixi.toml",
]


@pytest.fixture(scope="session")
def bump():
    return load_script(BUMP_PATH, "bump_af_version")


@pytest.fixture()
def values_text():
    return VALUES_PATH.read_text()


@pytest.fixture()
def sync_file(tmp_path):
    path = tmp_path / "deployment.yaml"
    path.write_text(SYNC_PATH.read_text())
    return path


@pytest.fixture()
def pixi_files(tmp_path):
    paths = []
    for src in PIXI_PATHS:
        path = tmp_path / src.parent.name / "pixi.toml"
        path.parent.mkdir()
        path.write_text(src.read_text())
        paths.append(path)
    return paths


def _pixi_args(paths):
    return [arg for path in paths for arg in ("--pixi-file", str(path))]


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


def test_pixi_global_sync_runs_the_production_af_version(bump, values_text):
    cur = bump.current_version(values_text)
    assert f"{bump.DEFAULT_REGISTRY}:{cur}\n" in SYNC_PATH.read_text()


def test_apply_sync_rewrites_the_real_deployment(bump):
    new = bump.apply_sync(SYNC_PATH.read_text(), "9.9.9", bump.DEFAULT_REGISTRY)
    assert f"image: {bump.DEFAULT_REGISTRY}:9.9.9\n" in new


def test_apply_sync_fails_loudly_on_layout_drift(bump):
    with pytest.raises(SystemExit, match="pixi-global-sync"):
        bump.apply_sync("image: busybox\n", "9.9.9", bump.DEFAULT_REGISTRY)


@pytest.mark.parametrize("path", PIXI_PATHS, ids=lambda p: p.parent.name)
def test_main_carries_the_dev_version_of_the_production_release(
    bump, values_text, path
):
    """Only a release commit, under its tag, carries a bare version."""
    cur = bump.current_version(values_text)
    assert f'\nversion = "{cur}+dev"\n' in path.read_text()


@pytest.mark.parametrize("path", PIXI_PATHS, ids=lambda p: p.parent.name)
@pytest.mark.parametrize("version", ["9.9.9", "9.9.9+dev"])
def test_apply_pixi_rewrites_the_real_manifests(bump, path, version):
    text = path.read_text()
    new = bump.apply_pixi(text, version)
    assert f'\nversion = "{version}"\n' in new
    assert len(new.splitlines()) == len(text.splitlines())
    assert bump.apply_pixi(new, "10.0.0").count('version = "10.0.0"') == 1


def test_apply_pixi_leaves_dependency_versions_alone(bump):
    text = '[workspace]\nversion = "0.1.2+dev"\n\n[dependencies]\nversion = "1.0.0"\n'
    with pytest.raises(SystemExit, match="exactly 1 workspace version"):
        bump.apply_pixi(text, "9.9.9")
    with pytest.raises(SystemExit, match="exactly 1 workspace version"):
        bump.apply_pixi("[workspace]\n", "9.9.9")


# ── main() CLI ────────────────────────────────────────────────────────────────


def test_main_print_current(bump, tmp_path, capsys, values_text, monkeypatch):
    path = tmp_path / "values.yaml"
    path.write_text(values_text)
    cur = bump.current_version(values_text)

    _run_main(bump, monkeypatch, "--print-current", "--file", str(path))

    assert capsys.readouterr().out.strip() == cur
    assert path.read_text() == values_text  # unchanged


def test_main_bump_dry_run(bump, tmp_path, capsys, values_text, sync_file, monkeypatch):
    path = tmp_path / "values.yaml"
    path.write_text(values_text)
    sync_before = sync_file.read_text()
    cur = bump.current_version(values_text)
    expected = bump.bump_version(cur, "patch")

    _run_main(
        bump,
        monkeypatch,
        "--bump",
        "patch",
        "--file",
        str(path),
        "--sync-file",
        str(sync_file),
        "--dry-run",
    )

    out = capsys.readouterr()
    assert out.out.strip() == expected
    assert f"{cur} -> {expected}" in out.err
    assert "dry run" in out.err
    assert path.read_text() == values_text
    assert sync_file.read_text() == sync_before


def test_main_set_writes_every_file(
    bump, tmp_path, capsys, values_text, sync_file, pixi_files, monkeypatch
):
    path = tmp_path / "values.yaml"
    path.write_text(values_text)
    global_dir = pixi_files[1].parent
    (global_dir / "pixi.lock").write_text("the lock\n")
    release_dir = tmp_path / "release"

    _run_main(
        bump,
        monkeypatch,
        "--set",
        "9.9.9",
        "--file",
        str(path),
        "--sync-file",
        str(sync_file),
        "--global-dir",
        str(global_dir),
        "--release-dir",
        str(release_dir),
        *_pixi_args(pixi_files),
    )

    out = capsys.readouterr()
    assert out.out.strip() == "9.9.9"
    assert 'docker_image_tag: "9.9.9"' in path.read_text()
    assert "/purdue-af:9.9.9\n" in sync_file.read_text()
    for pixi in pixi_files:
        assert '\nversion = "9.9.9"\n' in pixi.read_text()
    assert (release_dir / "pixi.toml").read_text() == pixi_files[1].read_text()
    assert (release_dir / "pixi.lock").read_text() == "the lock\n"


def test_release_copy_is_the_global_env_but_its_version(bump):
    """pixi-global-sync installs a stamped manifest and a lock from here."""
    release = REPO / "apps" / "af-utils" / "pixi-global-sync" / "release"
    toml = (release / "pixi.toml").read_text()
    assert re.search(r'(?m)^version = "\d+\.\d+\.\d+(\+dev)?"$', toml)
    assert (release / "pixi.lock").read_text().startswith("version: ")


def test_main_mark_dev_writes_only_the_pixi_manifests(
    bump, tmp_path, capsys, values_text, pixi_files, monkeypatch
):
    path = tmp_path / "values.yaml"
    path.write_text(values_text)
    for pixi in pixi_files:
        pixi.write_text(bump.apply_pixi(pixi.read_text(), "9.9.9"))
    path.write_text(bump.apply(values_text, "9.9.9", bump.DEFAULT_REGISTRY))
    values_before = path.read_text()
    release_before = (REPO / bump.DEFAULT_RELEASE_DIR / "pixi.toml").read_text()

    _run_main(
        bump, monkeypatch, "--mark-dev", "--file", str(path), *_pixi_args(pixi_files)
    )

    assert capsys.readouterr().out.strip() == "9.9.9+dev"
    assert path.read_text() == values_before
    assert (REPO / bump.DEFAULT_RELEASE_DIR / "pixi.toml").read_text() == release_before
    for pixi in pixi_files:
        assert '\nversion = "9.9.9+dev"\n' in pixi.read_text()


def test_main_mark_dev_dry_run(
    bump, tmp_path, capsys, values_text, pixi_files, monkeypatch
):
    path = tmp_path / "values.yaml"
    path.write_text(values_text)
    before = [pixi.read_text() for pixi in pixi_files]
    _run_main(
        bump,
        monkeypatch,
        "--mark-dev",
        "--file",
        str(path),
        "--dry-run",
        *_pixi_args(pixi_files),
    )
    assert "dry run" in capsys.readouterr().err
    assert [pixi.read_text() for pixi in pixi_files] == before


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
