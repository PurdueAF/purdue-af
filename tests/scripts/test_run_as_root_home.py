"""Tests for the home-directory setup in docker/purdue-af/scripts/run-as-root.sh:
every write below the home goes through af_as_user, and none is fatal.

The script is trimmed to that section (munge and Slurm setup need a real
image), with the image paths pointed at a sandbox."""

import os
import re
import subprocess

import pytest
from common import REPO

SCRIPT = REPO / "docker" / "purdue-af" / "scripts" / "run-as-root.sh"
HELPER = REPO / "docker" / "purdue-af" / "scripts" / "af-as-user.sh"

ID_ROOT_STUB = """#!/bin/bash
if [ "$1" = "-u" ]; then echo 0; exit 0; fi
exec /usr/bin/id "$@"
"""

RUNUSER_STUB = """#!/bin/bash
printf '%s\\n' "$*" >> "$RUNUSER_LOG"
shift 3
exec "$@"
"""

# chown/chmod to a user the test host does not have; the calls must be
# survivable anyway, so stub them rather than skipping the assertions.
NOOP_STUB = "#!/bin/bash\nexit 0\n"


@pytest.fixture()
def home_setup(tmp_path):
    """The home-directory section of the hook, ending before the Slurm and
    kernel work that needs the image."""
    home = tmp_path / "home" / "jovyan"
    home.mkdir(parents=True)

    body = SCRIPT.read_text()
    cut = body.index('mkdir -p "/work/users/$NB_USER"')
    section = body[:cut].replace(
        "source /usr/local/bin/af-as-user.sh", f"source {HELPER}"
    )
    section = re.sub(
        r"^NEW_HOME=.*$", f'NEW_HOME="{home}"', section, count=1, flags=re.M
    )
    # The munge block reads /etc/secrets and would su to a user we do not have.
    section = section.replace(
        "if [ -f /etc/secrets/munge/munge.key ]; then", "if false; then"
    )

    script = tmp_path / "run-as-root-home.sh"
    script.write_text(section)
    return script, home, tmp_path


@pytest.fixture()
def run_hook(home_setup):
    script, home, tmp_path = home_setup
    bindir = tmp_path / "bin"
    bindir.mkdir()
    runuser_log = tmp_path / "runuser.log"

    def _run(as_root=False):
        for name in ("chown", "chmod"):
            (bindir / name).write_text(NOOP_STUB)
            (bindir / name).chmod(0o755)
        if as_root:
            runuser_log.write_text("")
            for name, body in (("id", ID_ROOT_STUB), ("runuser", RUNUSER_STUB)):
                (bindir / name).write_text(body)
                (bindir / name).chmod(0o755)
        return subprocess.run(
            ["bash", "-c", f"set -e; source {script}"],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "HOME": str(tmp_path),
                "NB_USER": "jovyan",
                "NB_UID": "1000",
                "RUNUSER_LOG": str(runuser_log),
            },
        )

    def _delegated():
        return [ln for ln in runuser_log.read_text().splitlines() if ln.strip()]

    _run.delegated = _delegated
    return _run, home


def test_home_directories_are_created(run_hook):
    run, home = run_hook
    result = run()
    assert result.returncode == 0, result.stderr
    assert (home / ".local/share/jupyter/runtime").is_dir()
    assert (home / ".jupyter/lab/workspaces").is_dir()
    assert (home / ".config/dask").is_dir()
    assert (home / ".jupyter/migrated").is_file()


def test_as_root_the_home_writes_are_delegated_to_the_user(run_hook):
    """Writes below the home go through af_as_user, never as root."""
    run, _ = run_hook
    result = run(as_root=True)
    assert result.returncode == 0, result.stderr

    calls = run.delegated()
    assert calls, "nothing was delegated: run-as-root.sh still writes as root"
    for needle in (".local/share/jupyter", ".jupyter", ".config/dask"):
        assert any(needle in c for c in calls), f"{needle} not delegated: {calls}"
    assert all(c.startswith("-u jovyan --") for c in calls), calls


def test_an_unwritable_dot_local_does_not_kill_the_session(run_hook, tmp_path):
    """The end-to-end property. A ~/.local that cannot be written — the depot
    symlink, reproduced with a read-only parent — must not exit the hook."""
    run, home = run_hook
    depot = tmp_path / "depot"
    depot.mkdir()
    (home / ".local").symlink_to(depot / "local")
    depot.chmod(0o500)
    try:
        result = run()
    finally:
        depot.chmod(0o700)

    assert result.returncode == 0, (
        "run-as-root.sh is sourced under `set -e`; a non-zero exit here is a "
        f"crash-looping notebook container.\nstderr:\n{result.stderr}"
    )
    # The rest of the home setup still happened.
    assert (home / ".jupyter/lab/workspaces").is_dir()


def test_the_shared_helper_is_shipped_and_sourced():
    """Both hooks source it by absolute path, so a missing COPY is a crash at
    spawn time, not a build failure — and config-extensions.sh is not part of
    the build smoke check (it reaches the network)."""
    dockerfile = (REPO / "docker" / "purdue-af" / "Dockerfile").read_text()
    assert "scripts/af-as-user.sh /usr/local/bin/" in dockerfile
    for hook in ("run-as-root.sh", "config-extensions.sh"):
        body = (REPO / "docker" / "purdue-af" / "scripts" / hook).read_text()
        assert "source /usr/local/bin/af-as-user.sh" in body, hook
        assert "af_as_user " in body, hook
