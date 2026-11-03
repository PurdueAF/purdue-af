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
from script_helpers import sandboxed

WRAPPER = REPO / "docker/purdue-af/pixi-wrapper"

BASE_ENV = "/opt/pixi"
GLOBAL_ENV = "/work/pixi/global"

# every project command the wrapper treats as writing to an environment
MUTATING = (
    "add",
    "import",
    "init",
    "install",
    "publish",
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
        sandboxed(WRAPPER, {'REAL_PIXI="/opt/pixi/bin/pixi"': f'REAL_PIXI="{stub}"'})
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


@pytest.fixture()
def linked_shared_env(tmp_path, run_pixi):
    """A wrapper whose shared env is a sandbox directory, and a symlink to it."""
    real = tmp_path / "shared"
    real.mkdir()
    (real / "pixi.toml").write_text("[project]\n")
    link = tmp_path / "home-link"
    link.symlink_to(real)

    script = tmp_path / "pixi-scoped"
    script.write_text(
        sandboxed(
            tmp_path / "pixi",
            {'GLOBAL_ENV_PROJECT="/work/pixi/global"': f'GLOBAL_ENV_PROJECT="{real}"'},
        )
    )
    script.chmod(0o755)
    return script, link


@pytest.mark.parametrize(
    "command",
    [
        # `cd` through the link in a shell, so PWD is the logical path. Passing
        # cwd= to subprocess would not do: that chdirs, and bash then takes PWD
        # from getcwd(), which is already resolved — the guard would look correct
        # while doing nothing.
        'cd "{link}" && exec "{script}" add numpy',
        'exec "{script}" add numpy --manifest-path "{link}/pixi.toml"',
    ],
    ids=["working-directory", "manifest-path"],
)
def test_a_symlink_into_the_shared_env_is_still_refused(
    tmp_path, linked_shared_env, command
):
    """`~/work` is a symlink to `/work`, so the shared env is reachable from a
    path under /home, by working directory or by argument. Resolving the project
    directory is what makes the guard match the directory rather than the
    spelling — and what keeps the answer from being the unrelated "move it off
    /home"."""
    script, link = linked_shared_env
    result = subprocess.run(
        ["bash", "-c", command.format(link=link, script=script)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 1, "reached through a symlink, the guard missed it"
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


# --- resolved paths --------------------------------------------------------


@pytest.fixture()
def scoped_wrapper(tmp_path):
    """The wrapper with /home and the shared env moved into a real sandbox, and a
    runner that `cd`s in a shell so PWD stays the logical path."""
    home = tmp_path / "home"
    (home / "u").mkdir(parents=True)
    work = tmp_path / "work"
    (work / "users" / "u" / "proj").mkdir(parents=True)
    (work / "pixi" / "global").mkdir(parents=True)
    (work / "pixi" / "global" / "pixi.toml").write_text("[project]\n")
    (work / "users" / "u" / "proj" / "pixi.toml").write_text("[project]\n")
    (home / "u" / "work").symlink_to(work)

    stub = tmp_path / "pixi-stub"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o755)
    script = tmp_path / "pixi-scoped"
    script.write_text(
        sandboxed(
            WRAPPER,
            {
                'REAL_PIXI="/opt/pixi/bin/pixi"': f'REAL_PIXI="{stub}"',
                'HOME_PREFIX="/home/"': f'HOME_PREFIX="{home}/"',
                'GLOBAL_ENV_PROJECT="/work/pixi/global"': (
                    f'GLOBAL_ENV_PROJECT="{work}/pixi/global"'
                ),
            },
        )
    )
    script.chmod(0o755)

    def _run(cd, *args):
        command = f'cd "{cd}" && exec "{script}" "$@"'
        return subprocess.run(
            ["bash", "-c", command, "pixi", *args],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(home / "u")},
        )

    return _run, home / "u", work


def test_a_work_project_entered_through_the_home_symlink_is_allowed(scoped_wrapper):
    run, home, _ = scoped_wrapper
    result = run(home / "work/users/u/proj", "add", "numpy")
    assert result.returncode == 0, result.stderr


def test_a_work_manifest_named_through_the_home_symlink_is_allowed(scoped_wrapper):
    run, home, _ = scoped_wrapper
    manifest = home / "work/users/u/proj/pixi.toml"
    result = run(home, "add", "numpy", "--manifest-path", str(manifest))
    assert result.returncode == 0, result.stderr


def test_the_shared_env_through_the_home_symlink_is_still_refused(scoped_wrapper):
    run, home, _ = scoped_wrapper
    result = run(home / "work/pixi/global", "add", "numpy")
    assert result.returncode == 1
    assert "reconciled automatically" in result.stderr


@pytest.mark.parametrize("manifest", ["pixi.toml", "./pixi.toml", "pyproject.toml"])
def test_a_relative_manifest_under_home_is_refused(scoped_wrapper, manifest):
    run, home, _ = scoped_wrapper
    (home / "proj").mkdir()
    (home / "proj" / manifest).write_text("[project]\n")
    result = run(home / "proj", "add", "numpy", "--manifest-path", manifest)
    assert result.returncode == 1
    assert "not allowed for projects under" in result.stderr


def test_a_relative_manifest_that_does_not_exist_yet_is_refused_under_home(
    scoped_wrapper,
):
    run, home, _ = scoped_wrapper
    result = run(home, "init", "--manifest-path", "new/pixi.toml")
    assert result.returncode == 1
    assert "not allowed for projects under" in result.stderr


def test_a_bare_manifest_inside_the_shared_env_is_refused(scoped_wrapper):
    run, _, work = scoped_wrapper
    result = run(work / "pixi/global", "add", "numpy", "--manifest-path", "pixi.toml")
    assert result.returncode == 1
    assert "reconciled automatically" in result.stderr


def test_a_link_from_work_into_a_home_project_is_refused(scoped_wrapper):
    """The other direction: the spelling is off /home, the project is not."""
    run, home, work = scoped_wrapper
    (home / "proj").mkdir()
    (home / "proj" / "pixi.toml").write_text("[project]\n")
    (work / "users/u/link").symlink_to(home / "proj")
    result = run(work / "users/u/link", "install")
    assert result.returncode == 1
    assert "not allowed for projects under" in result.stderr
