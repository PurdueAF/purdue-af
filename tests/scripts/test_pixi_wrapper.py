"""Tests for docker/purdue-af/pixi-wrapper — the `pixi` on a session's PATH.

The wrapper is a guardrail, not a package manager: it refuses the handful of
commands that would fail confusingly or damage something shared, and passes
everything else straight through. These tests run it with the real pixi
replaced by a stub that records its argv, so "was it refused?" and "was it
passed through?" are both directly observable.
"""

import subprocess

import pytest
from common import REPO

WRAPPER = REPO / "docker/purdue-af/pixi-wrapper"

BASE_ENV = "/opt/pixi"
GLOBAL_ENV = "/work/pixi/global"

# every project command the wrapper treats as writing to an environment
MUTATING = (
    "add",
    "build",
    "import",
    "init",
    "install",
    "reinstall",
    "remove",
    "update",
    "upgrade",
    "upload",
)

STUB = """#!/bin/bash
printf '%s\\n' "$*" > "$PIXI_STUB_LOG"
exit 0
"""


@pytest.fixture()
def run_pixi(tmp_path):
    """Run the wrapper with the real pixi stubbed; returns (result, argv|None).

    Only the path of the real binary is rewritten — every guard under test is
    the wrapper's own code, unmodified.
    """
    log = tmp_path / "pixi.argv"
    stub = tmp_path / "pixi-stub"
    stub.write_text(STUB)
    stub.chmod(0o755)
    script = tmp_path / "pixi"
    script.write_text(
        WRAPPER.read_text().replace(
            'REAL_PIXI="/opt/pixi/bin/pixi"', f'REAL_PIXI="{stub}"'
        )
    )
    script.chmod(0o755)

    def _run(*args, cwd=None, **env):
        if log.exists():
            log.unlink()
        result = subprocess.run(
            [str(script), *args],
            capture_output=True,
            text=True,
            cwd=str(cwd or tmp_path),
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "PIXI_STUB_LOG": str(log),
                **env,
            },
        )
        passed = log.read_text().strip() if log.exists() else None
        return result, passed

    return _run


# --- the platform's own environments --------------------------------------


@pytest.mark.parametrize("subcmd", MUTATING)
@pytest.mark.parametrize("target", [BASE_ENV, GLOBAL_ENV])
def test_mutating_a_platform_environment_is_refused(run_pixi, subcmd, target):
    """The base env belongs to the image and the shared env is reconciled from
    the platform repository — a package added to either is lost, in one case
    silently. Refuse before pixi does anything."""
    result, passed = run_pixi(subcmd, "--manifest-path", f"{target}/pixi.toml")
    assert result.returncode == 1, result.stdout
    assert passed is None, "the command reached pixi anyway"
    assert target in result.stderr


@pytest.mark.parametrize("target", [BASE_ENV, GLOBAL_ENV])
def test_the_refusal_says_where_to_go_instead(run_pixi, target):
    """A guardrail that only says no leaves the user, or an agent, to guess at
    the next thing to try."""
    result, _ = run_pixi("add", "numpy", "--manifest-path", f"{target}/pixi.toml")
    assert "pixi init" in result.stderr
    assert "/work/users/" in result.stderr


def test_the_shared_env_refusal_names_the_route_for_everyone(run_pixi):
    """Adding a package for the whole facility is a real request with a real
    answer; it just is not this command."""
    result, _ = run_pixi("add", "numpy", "--manifest-path", f"{GLOBAL_ENV}/pixi.toml")
    assert "pixi/global/pixi.toml" in result.stderr


@pytest.mark.parametrize("subcmd", ["shell", "shell-hook"])
@pytest.mark.parametrize("target", [BASE_ENV, GLOBAL_ENV])
def test_activating_a_platform_environment_is_allowed(run_pixi, subcmd, target):
    """Using the shared environment is the point of having one. Only changing
    it is refused."""
    result, passed = run_pixi(subcmd, "--manifest-path", f"{target}/pixi.toml")
    assert result.returncode == 0, result.stderr
    assert passed is not None and subcmd in passed


@pytest.mark.parametrize("subcmd", ["run", "info", "list", "tree", "--version"])
def test_read_only_commands_pass_through(run_pixi, subcmd):
    result, passed = run_pixi(subcmd, "--manifest-path", f"{GLOBAL_ENV}/pixi.toml")
    assert result.returncode == 0, result.stderr
    assert passed is not None


def test_a_symlink_into_the_shared_env_is_still_refused(tmp_path, run_pixi):
    """`~/work` is a symlink to `/work`, so the shared env is reachable from a
    path under /home. Resolving the project directory is what makes the guard
    match the directory rather than the spelling — and what keeps the answer
    from being the unrelated "move it off /home"."""
    real = tmp_path / "shared"
    real.mkdir()
    (real / "pixi.toml").write_text("[project]\n")
    link = tmp_path / "home-link"
    link.symlink_to(real)

    script = tmp_path / "pixi-scoped"
    script.write_text(
        (tmp_path / "pixi")
        .read_text()
        .replace(
            'GLOBAL_ENV_PROJECT="/work/pixi/global"', f'GLOBAL_ENV_PROJECT="{real}"'
        )
    )
    script.chmod(0o755)
    # `cd` through the link in a shell, so PWD is the logical path. Passing
    # cwd= to subprocess would not do: that chdirs, and bash then takes PWD
    # from getcwd(), which is already resolved — the guard would look correct
    # while doing nothing.
    result = subprocess.run(
        ["bash", "-c", f'cd "{link}" && exec "{script}" add numpy'],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 1, "reached through a symlink, the guard missed it"
    assert "reconciled automatically" in result.stderr


def test_a_symlinked_manifest_path_into_the_shared_env_is_refused(tmp_path, run_pixi):
    """The same evasion by argument rather than by working directory."""
    real = tmp_path / "shared2"
    real.mkdir()
    (real / "pixi.toml").write_text("[project]\n")
    link = tmp_path / "link2"
    link.symlink_to(real)

    script = tmp_path / "pixi-scoped2"
    script.write_text(
        (tmp_path / "pixi")
        .read_text()
        .replace(
            'GLOBAL_ENV_PROJECT="/work/pixi/global"', f'GLOBAL_ENV_PROJECT="{real}"'
        )
    )
    script.chmod(0o755)
    result = subprocess.run(
        [str(script), "add", "numpy", "--manifest-path", f"{link}/pixi.toml"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 1, "a symlinked --manifest-path slipped past"
    assert "reconciled automatically" in result.stderr


def test_an_ordinary_project_is_untouched(tmp_path, run_pixi):
    """The guard must not cost anyone their own project on /work."""
    project = tmp_path / "work" / "users" / "someone" / "analysis"
    project.mkdir(parents=True)
    (project / "pixi.toml").write_text("[project]\n")
    result, passed = run_pixi("add", "numpy", cwd=project)
    assert result.returncode == 0, result.stderr
    assert passed == "add numpy"


# --- the pre-existing /home guards, which had no tests --------------------


def test_a_project_under_home_is_still_refused(run_pixi):
    """Pixi environments would exhaust the 25 GB home quota."""
    result, passed = run_pixi("add", "--manifest-path", "/home/someone/proj/pixi.toml")
    assert result.returncode == 1
    assert passed is None
    assert "/home/" in result.stderr


def test_pixi_home_under_home_is_still_refused(run_pixi):
    result, passed = run_pixi("global", "install", "cowsay", PIXI_HOME="/home/u/.pixi")
    assert result.returncode == 1
    assert passed is None
    assert "PIXI_HOME" in result.stderr


def test_pixi_global_off_home_is_allowed(run_pixi):
    """The spawner presets PIXI_HOME under /work/users/<user>, so a user's own
    global installs are normal and must keep working."""
    result, passed = run_pixi(
        "global", "install", "cowsay", PIXI_HOME="/work/users/u/.pixi-home"
    )
    assert result.returncode == 0, result.stderr
    assert passed == "global install cowsay"
