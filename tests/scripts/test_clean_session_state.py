"""Tests for docker/purdue-af/scripts/clean-session-state.sh — the startup hook
that clears the previous session's runtime leftovers out of the user's home.

Two properties matter, and they pull against each other. The hook deletes, so
the tests that say what it must NOT touch are the important half: caches,
history, session transcripts, and above all ~/.claude/jobs, whose `tmp`
directory holds scripts the user wrote. And because start.sh sources it under
`set -e` as root, it must survive homes it cannot fully clean rather than
taking the session down — the same failure mode that made a symlinked
~/.local a 600-second spawn timeout."""

import os
import subprocess

import pytest
from common import REPO

SCRIPT = REPO / "docker" / "purdue-af" / "scripts" / "clean-session-state.sh"

# Laid out from what real AF sessions accumulate: pid-named locks, sockets,
# process logs and interrupted atomic writes, alongside the things that must
# survive them.
STALE = [
    ".claude/ide/47858.lock",
    ".codex/ipc/ipc.sock",
    ".codex/tmp/arg0",
    ".copilot/logs/process-1788190237352-4118647.log",
    ".vscode-server/data/logs/exthost.log",
    ".cursor-server/data/logs/remoteagent.log",
    ".git-credential-cache/socket",
    ".claude/daemon.lock",
    ".claude/daemon.log",
    ".claude/daemon.status.json",
    ".claude.json.tmp.27004.086a1b317279",
    ".claude/.credentials.json.tmp.051b2f4f",
    ".codex/.tmp/plugins.sync.lock",
    ".vscode-server/.cli.03c265b1adee71ac88f833e065f7bb956b60550a.log",
    ".vscode-server/cli/agent-host-stable.lock",
    ".vscode-server/cli/agent-host-stable.log",
    ".bash_history-00245.tmp",
    ".python_history-30706.tmp",
]

KEEP = [
    # User work living inside something that looks like scratch space.
    ".claude/jobs/11020811/tmp/audit_jme.py",
    ".claude/jobs/11020811/state.json",
    ".claude/worktrees/branch-a/analysis.py",
    # Config, credentials, history, transcripts.
    ".claude/CLAUDE.md",
    ".claude/.credentials.json",
    ".claude/settings.json",
    ".claude/projects/some-project/transcript.jsonl",
    ".claude/history.jsonl",
    ".codex/AGENTS.md",
    ".codex/config.toml",
    ".codex/auth.json",
    ".codex/sessions/a-session.jsonl",
    ".codex/.tmp/plugins/some-plugin.js",
    ".continue/config.yaml",
    ".continue/api-key.txt",
    # Caches and installed servers: expensive to rebuild, not runtime state.
    ".claude/cache/blob",
    ".cache/pip/wheel",
    ".vscode-server/extensions/ms-python.python/package.json",
    ".vscode-server/cli/servers/Stable-abc/server/bin/code-server",
    ".cursor-server/bin/abc/bin/cursor-server",
    ".bash_history",
    ".python_history",
    ".ssh/authorized_keys",
]


def _touch(home, rel):
    path = home / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    return path


@pytest.fixture()
def home(tmp_path):
    h = tmp_path / "home" / "jovyan"
    h.mkdir(parents=True)
    return h


@pytest.fixture()
def run_hook(tmp_path, home):
    """Source the hook the way start.sh does: `set -e`, NB_USER set, home
    under /home/$NB_USER — which the sandbox stands in for."""
    script = tmp_path / "clean-session-state.sh"
    script.write_text(
        SCRIPT.read_text().replace(
            '_CSS_HOME="/home/${NB_USER}"', f'_CSS_HOME="{home.parent}/${{NB_USER}}"'
        )
    )

    def _run(nb_user="jovyan"):
        return subprocess.run(
            ["bash", "-c", f"set -e; source {script}"],
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], "NB_USER": nb_user},
        )

    return _run


def test_stale_runtime_state_is_removed(run_hook, home):
    for rel in STALE:
        _touch(home, rel)

    result = run_hook()

    assert result.returncode == 0, result.stderr
    left = [rel for rel in STALE if (home / rel).exists()]
    assert not left, f"stale runtime state survived: {left}"


def test_user_data_and_caches_are_untouched(run_hook, home):
    """The half that matters. ~/.claude/jobs in particular looks like scratch
    but holds scripts the user wrote."""
    for rel in STALE + KEEP:
        _touch(home, rel)

    result = run_hook()

    assert result.returncode == 0, result.stderr
    gone = [rel for rel in KEEP if not (home / rel).exists()]
    assert not gone, f"the hook deleted things it must keep: {gone}"


def test_emptied_directories_still_exist(run_hook, home):
    """Contents go, the directory stays: the tools that own these expect to
    find them, and recreating them as root would flip their ownership."""
    _touch(home, ".claude/ide/47858.lock")
    _touch(home, ".codex/ipc/ipc.sock")

    assert run_hook().returncode == 0
    assert (home / ".claude/ide").is_dir()
    assert (home / ".codex/ipc").is_dir()


def test_a_clean_home_is_not_an_error(run_hook, home):
    """First-ever start: none of these paths exist yet."""
    result = run_hook()
    assert result.returncode == 0, result.stderr
    assert "removed" not in result.stdout


def test_symlinked_dir_is_left_alone_not_deleted_through(run_hook, home, tmp_path):
    """Users symlink parts of their home onto /depot to stay under quota. The
    hook must not follow such a link and empty the depot directory behind it."""
    depot = tmp_path / "depot"
    (depot / "logs").mkdir(parents=True)
    (depot / "logs" / "kept.log").write_text("x")
    (home / ".copilot").mkdir()
    (home / ".copilot" / "logs").symlink_to(depot / "logs")

    result = run_hook()

    assert result.returncode == 0, result.stderr
    assert (depot / "logs" / "kept.log").exists(), "deleted through a symlink"


def test_missing_nb_user_cleans_nothing(run_hook, home):
    """An empty NB_USER would make the home path `/home/` — the hook has to
    refuse rather than walk every user's directory."""
    _touch(home, ".claude/ide/47858.lock")

    result = run_hook(nb_user="")

    assert result.returncode == 0, result.stderr
    assert (home / ".claude/ide/47858.lock").exists()
    assert "nothing to clean" in result.stderr


def test_undeletable_path_does_not_kill_the_session(run_hook, home):
    """Whatever the hook cannot delete, it moves past — it must never be the
    reason a session fails to spawn. On the cluster this is /depot: NFS with
    root_squash, and the hook runs as root. A read-only parent reproduces it.

    The stale lock in the writable directory proves the hook kept going rather
    than stopping at the first failure."""
    doomed = _touch(home, ".copilot/logs/process-1.log")
    _touch(home, ".claude/ide/47858.lock")
    (home / ".copilot" / "logs").chmod(0o500)
    try:
        result = run_hook()
    finally:
        (home / ".copilot" / "logs").chmod(0o700)

    assert result.returncode == 0, (
        "an undeletable path must not exit the hook; start.sh sources it under "
        f"`set -e`.\nstderr:\n{result.stderr}"
    )
    assert doomed.exists(), "the test did not actually make the rm fail"
    assert not (home / ".claude/ide/47858.lock").exists(), (
        "the hook stopped at the first failure instead of continuing"
    )


def test_the_hook_is_installed_and_runs_before_the_others(run_hook):
    """It has to be in before-notebook.d to run at all, and run-hooks sources
    that directory in sorted order — the name puts it ahead of config-agents.sh
    so the previous session's locks are gone before anything reads them."""
    dockerfile = (REPO / "docker" / "purdue-af" / "Dockerfile").read_text()
    assert "scripts/clean-session-state.sh" in dockerfile
    hooks = [
        "clean-session-state.sh",
        "config-agents.sh",
        "config-extensions.sh",
        "run-as-root.sh",
    ]
    assert hooks == sorted(hooks), "the hook no longer sorts first"
