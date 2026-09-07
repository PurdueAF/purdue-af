"""Tests for docker/purdue-af/scripts/config-extensions.sh — the startup hook
that configures JupyterLab and pre-installs the code-server extensions.

start.sh *sources* this hook, under `set -e`, as root. That combination is the
whole risk: any command that exits non-zero here takes the container down
before JupyterLab binds its port, and the hub reports that only as "server
didn't respond in 600 seconds" — a message that points nowhere near the real
cause. These tests run it the way start.sh does, so a regression shows up as a
non-zero exit.

The directories the hook creates live in the user's own $HOME, so it drops to
the user to create them. That is what makes a depot-backed ~/.local work at all
— /depot is NFS with root_squash, so root is the one identity that cannot write
there — and the tests below pin it, since running as root would look correct
everywhere except on the homes that actually broke.

The paths that only exist inside the image are redirected at a sandbox; the
`code-server` and `chown` the hook shells out to are replaced with stubs."""

import os
import subprocess

import pytest
from common import REPO

SCRIPT = REPO / "docker" / "purdue-af" / "scripts" / "config-extensions.sh"

CODE_SERVER_STUB = """#!/bin/bash
# --list-extensions prints nothing: every extension then looks missing and the
# hook takes its install path, which is the branch worth exercising.
for arg in "$@"; do
    if [ "$arg" = "--list-extensions" ]; then exit 0; fi
done
exit 0
"""

NOOP_STUB = "#!/bin/bash\nexit 0\n"

# Stubs that let the tests take the root branch of _cs_as_user without root:
# `id -u` reports 0, and `runuser -u <user> -- cmd...` logs the call and then
# runs the command as the (unprivileged) test user.
ID_ROOT_STUB = """#!/bin/bash
if [ "$1" = "-u" ]; then echo 0; exit 0; fi
exec /usr/bin/id "$@"
"""

RUNUSER_STUB = """#!/bin/bash
# runuser -u <user> -- <cmd> ...
printf '%s\\n' "$*" >> "$RUNUSER_LOG"
shift 3
exec "$@"
"""


@pytest.fixture()
def sandbox(tmp_path):
    """The image paths the hook writes to, redirected at tmp_path."""
    home = tmp_path / "home" / "jovyan"
    home.mkdir(parents=True)

    base_env = tmp_path / "base-env"
    (base_env / "bin").mkdir(parents=True)
    code_server = base_env / "bin" / "code-server"
    code_server.write_text(CODE_SERVER_STUB)
    code_server.chmod(0o755)

    continue_config = tmp_path / "continue-config.yaml"
    continue_config.write_text("models:\n  - apiKey: PLACEHOLDER\n")

    script = tmp_path / "config-extensions.sh"
    script.write_text(
        SCRIPT.read_text()
        .replace(
            "base_env_dir=/opt/pixi/.pixi/envs/base-env/",
            f"base_env_dir={base_env}/",
        )
        .replace("NEW_HOME=/home/$NB_USER", f"NEW_HOME={home}")
        .replace(
            "source /usr/local/bin/af-as-user.sh",
            f"source {REPO / 'docker/purdue-af/scripts/af-as-user.sh'}",
        )
        .replace("/etc/jupyter/continue-config.yaml", str(continue_config))
    )
    return script, home, tmp_path


@pytest.fixture()
def run_hook(sandbox):
    """Source the hook the way start.sh does — `set -e`, so any unguarded
    failure surfaces as a non-zero return code."""
    script, home, tmp_path = sandbox

    bindir = tmp_path / "bin"
    bindir.mkdir()
    # The hook chowns to NB_USER:users; the tests do not run as root.
    for name in ("chown",):
        stub = bindir / name
        stub.write_text(NOOP_STUB)
        stub.chmod(0o755)

    runuser_log = tmp_path / "runuser.log"

    def _run(as_root=False):
        """as_root=True stubs `id` and `runuser` so the hook takes the same
        branch it takes in a real session, and records what it delegated."""
        if as_root:
            runuser_log.write_text("")
            for name, body in (("id", ID_ROOT_STUB), ("runuser", RUNUSER_STUB)):
                stub = bindir / name
                stub.write_text(body)
                stub.chmod(0o755)
        return subprocess.run(
            ["bash", "-c", f"set -e; source {script}"],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "HOME": str(tmp_path),
                "NB_USER": "jovyan",
                "JUPYTER_IMAGE": "purdueaf/purdue-af:0.13.4",
                "HOSTNAME": "purdue-af-1",
                "RUNUSER_LOG": str(runuser_log),
            },
        )

    def _delegated():
        return [ln for ln in runuser_log.read_text().splitlines() if ln.strip()]

    _run.delegated = _delegated
    return _run, home


def test_code_server_dirs_are_created_under_dot_local(run_hook):
    """The happy path: .local is an ordinary directory, so the hook lays out
    the code-server dirs and runs to completion."""
    run, home = run_hook
    result = run()

    assert result.returncode == 0, result.stderr
    assert (home / ".local/share/code-server/extensions").is_dir()
    assert (home / ".local/share/code-server/User/settings.json").is_file()
    assert "skipping code-server setup" not in result.stderr


def test_unwritable_dot_local_does_not_kill_the_session(run_hook):
    """The regression this guard exists for.

    Users symlink ~/.local onto /depot to keep it out of the home quota. /depot
    is NFS with root_squash and this hook runs as root, so mkdir through that
    symlink returns EACCES. Before the guard, `set -e` turned that into an exit
    from start.sh, the notebook container crash-looped, and the only symptom
    the user or the hub log ever saw was a 600-second spawn timeout.

    A read-only parent reproduces the EACCES without needing NFS."""
    run, home = run_hook
    depot = home.parent / "depot"
    (depot / "home_overflow").mkdir(parents=True)
    (home / ".local").symlink_to(depot / "home_overflow" / ".local")
    depot.chmod(0o500)

    try:
        result = run()
    finally:
        depot.chmod(0o700)

    assert result.returncode == 0, (
        "the hook must survive an unwritable ~/.local; a non-zero exit here is "
        f"a crash-looping notebook container.\nstderr:\n{result.stderr}"
    )
    assert "skipping code-server setup" in result.stderr
    # Execution reached the end of the hook: the setup that follows the
    # code-server block still ran, so JupyterLab comes up without the editor.
    assert (home / ".continue" / "config.yaml").is_file()


def test_jupyterlab_config_is_written_regardless(run_hook):
    """JupyterLab's own settings must not depend on the code-server setup."""
    run, home = run_hook
    result = run()

    assert result.returncode == 0, result.stderr
    topbar = home / ".jupyter/lab/user-settings/jupyterlab-topbar-text"
    assert (topbar / "plugin.jupyterlab-settings").is_file()
    assert "0.13.4" in (topbar / "plugin.jupyterlab-settings").read_text()


def test_as_root_the_writes_are_delegated_to_the_user(run_hook):
    """The heart of the fix. Running as root, every write into the user's home
    must go through `runuser -u $NB_USER`. Doing it as root works on an
    ordinary home and fails on exactly the ones that broke — a ~/.local
    symlinked onto /depot, which is NFS with root_squash."""
    run, _ = run_hook
    result = run(as_root=True)
    assert result.returncode == 0, result.stderr

    calls = run.delegated()
    assert calls, "nothing was delegated: the hook still writes as root"

    # Every area of the home the hook touches, not just code-server: the same
    # trap applies to whichever directory a user has moved onto depot.
    for needle in (
        ".jupyter/lab/user-settings",  # topbar + grafana panel settings
        ".local/share/code-server",  # extensions and user data
        ".continue",  # bundled Continue config
    ):
        assert any(needle in c for c in calls), f"{needle} not delegated: {calls}"

    assert any("mkdir -p" in c for c in calls), calls
    assert any("tee" in c and "settings.json" in c for c in calls), calls
    assert any("--install-extension" in c for c in calls), calls
    # Always to the notebook user, never anyone else.
    assert all(c.startswith("-u jovyan --") for c in calls), calls


def test_as_root_nothing_in_the_home_is_left_owned_by_root(run_hook):
    """The corollary: run-as-root.sh exists partly to chown back what this hook
    used to create as root. Delegating means there is nothing to repair."""
    run, home = run_hook
    assert run(as_root=True).returncode == 0
    settings = home / ".local/share/code-server/User/settings.json"
    assert settings.is_file()
    assert "purdueaf.jupyterLabPath" in settings.read_text()
    # Written through the delegation path too, not just created there.
    topbar = home / ".jupyter/lab/user-settings/jupyterlab-topbar-text"
    assert (topbar / "plugin.jupyterlab-settings").is_file()
    assert (home / ".continue" / "config.yaml").is_file()
