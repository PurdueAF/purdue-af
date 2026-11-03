"""Tests for the dotfiles docker/purdue-af/scripts/run-as-root.sh leaves in a home:
.bashrc reaches .bashrc_af, a login shell reaches .bashrc, and what the user
wrote survives."""

import os
import subprocess

import pytest
from script_helpers import AF_IMAGE

SCRIPT = AF_IMAGE / "scripts" / "run-as-root.sh"
HELPER = AF_IMAGE / "scripts" / "af-as-user.sh"
STUB = "[ -f ~/.bashrc ] && . ~/.bashrc\n"


@pytest.fixture()
def dotfiles(tmp_path):
    """Run the hook's dotfile section against a sandbox home, under `set -e`."""
    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "chown").write_text("#!/bin/bash\nexit 0\n")
    (bindir / "chown").chmod(0o755)

    body = SCRIPT.read_text()
    section = body[body.index("# Fresh homes and older images") :]
    script = tmp_path / "dotfiles.sh"
    script.write_text(f'NEW_HOME="{home}"\nsource {HELPER}\n{section}')

    def _run():
        result = subprocess.run(
            ["bash", "-c", f"set -e; source {script}; echo SURVIVED"],
            capture_output=True,
            text=True,
            env={"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home)},
        )
        assert "SURVIVED" in result.stdout, result.stderr
        return result

    return _run, home


def test_a_fresh_home_gets_the_link_and_the_stub(dotfiles):
    run, home = dotfiles
    run()
    assert (home / ".bashrc").read_text() == f"source {home}/.bashrc_af\n"
    assert (home / ".bash_profile").read_text() == STUB


def test_a_login_shell_reaches_bashrc_af(dotfiles):
    run, home = dotfiles
    run()
    (home / ".bashrc_af").write_text("export FROM_AF=1\n")
    result = subprocess.run(
        [
            "/usr/bin/env",
            "-i",
            f"HOME={home}",
            "bash",
            "-l",
            "-c",
            'echo "af=${FROM_AF}"',
        ],
        capture_output=True,
        text=True,
    )
    assert "af=1" in result.stdout


def test_a_copy_an_older_image_wrote_becomes_the_stub(dotfiles):
    run, home = dotfiles
    copy = f"alias ll='ls -l'\nsource {home}/.bashrc_af\n"
    (home / ".bashrc").write_text(copy)
    (home / ".bash_profile").write_text(copy)
    run()
    assert (home / ".bash_profile").read_text() == STUB
    assert (home / ".bashrc").read_text() == copy


def test_a_users_own_bash_profile_survives_every_start(dotfiles):
    run, home = dotfiles
    mine = "export MINE=1\n. ~/.bashrc\n"
    (home / ".bash_profile").write_text(mine)
    run()
    run()
    assert (home / ".bash_profile").read_text() == mine


def test_the_bashrc_line_is_added_once(dotfiles):
    run, home = dotfiles
    (home / ".bashrc").write_text("# mine\n")
    run()
    run()
    assert (home / ".bashrc").read_text() == f"# mine\nsource {home}/.bashrc_af\n"


def test_profile_is_left_alone(dotfiles):
    run, home = dotfiles
    (home / ".profile").write_text("# mine\n")
    run()
    assert (home / ".profile").read_text() == "# mine\n"


def test_unwritable_dotfiles_are_not_fatal(dotfiles, tmp_path):
    """A dotfile symlinked onto a read-only target, as /depot is for root."""
    run, home = dotfiles
    depot = tmp_path / "depot"
    depot.mkdir()
    (home / ".bashrc").symlink_to(depot / "bashrc")
    (home / ".bash_profile").symlink_to(depot / "bash_profile")
    depot.chmod(0o500)
    try:
        result = run()
    finally:
        depot.chmod(0o700)
    assert "WARNING" in result.stderr


def test_a_link_at_bashrc_af_is_replaced_not_followed(tmp_path):
    """Root writes this file and chowns it to root; a planted link must not aim that elsewhere."""
    home = tmp_path / "home"
    home.mkdir()
    precious = tmp_path / "precious"
    precious.write_text("precious\n")
    (home / ".bashrc_af").symlink_to(precious)
    body = SCRIPT.read_text()
    start = body.index('bashrc_af_file="$NEW_HOME/.bashrc_af"')
    writer = body[start : body.index("\nEOF\n", start) + len("\nEOF\n")]
    subprocess.run(
        ["bash", "-c", f'NEW_HOME="{home}"\n{writer}'],
        env={"PATH": os.environ["PATH"], "NB_USER": "alice"},
        check=True,
    )
    assert precious.read_text() == "precious\n"
    assert not (home / ".bashrc_af").is_symlink()
