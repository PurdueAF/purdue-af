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


# --- CLI ------------------------------------------------------------------


@pytest.fixture()
def manifest(tmp_path):
    path = tmp_path / "deployment.yaml"
    path.write_text("          image: reg.example/purdueaf/agentic-interface:0.3.5\n")
    return path


def run_main(bump, monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["bump-agentic-version.py", *map(str, argv)])
    bump.main()


def test_print_current(bump, monkeypatch, capsys, manifest):
    run_main(bump, monkeypatch, "--print-current", "--file", manifest)
    assert capsys.readouterr().out == "0.3.5\n"


@pytest.mark.parametrize(
    "args,new", [(["--bump", "minor"], "0.4.0"), (["--set", "2.0.0"], "2.0.0")]
)
def test_release_rewrites_and_prints_only_the_version(
    bump, monkeypatch, capsys, manifest, args, new
):
    run_main(bump, monkeypatch, *args, "--file", manifest)
    captured = capsys.readouterr()
    assert captured.out == f"{new}\n"  # the workflow captures stdout
    assert f"0.3.5 -> {new}" in captured.err
    assert manifest.read_text().endswith(f"agentic-interface:{new}\n")


def test_dry_run_leaves_the_file(bump, monkeypatch, capsys, manifest):
    before = manifest.read_text()
    run_main(bump, monkeypatch, "--bump", "patch", "--dry-run", "--file", manifest)
    captured = capsys.readouterr()
    assert captured.out == "0.3.6\n"
    assert "dry run" in captured.err
    assert manifest.read_text() == before


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3", "1.2.3-rc1"])
def test_set_rejects_non_semver(bump, monkeypatch, manifest, bad):
    before = manifest.read_text()
    with pytest.raises(SystemExit, match="--set expects X.Y.Z"):
        run_main(bump, monkeypatch, "--set", bad, "--file", manifest)
    assert manifest.read_text() == before


def test_one_action_is_required(bump, monkeypatch, manifest):
    with pytest.raises(SystemExit):
        run_main(bump, monkeypatch, "--file", manifest)
