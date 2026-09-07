"""Tests for docker/purdue-af/scripts/config-extensions.sh — the startup hook
that configures JupyterLab and pre-installs the code-server extensions.

start.sh *sources* this hook, under `set -e`, as root. That combination is the
whole risk: any command that exits non-zero here takes the container down
before JupyterLab binds its port, and the hub reports that only as "server
didn't respond in 600 seconds" — a message that points nowhere near the real
cause. The hook therefore has to degrade rather than fail, and these tests run
it the way start.sh does so a regression shows up as a non-zero exit.

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

    def _run():
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
            },
        )

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
