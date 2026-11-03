"""Tests for run-hooks in docker/purdue-af/jupyter/start.sh: a hook that fails
costs the session that step, never the session.

The test shell inherits PATH: macOS /bin/bash 3.2 is not the bash of the image."""

import os
import re
import subprocess

import pytest
from script_helpers import AF_IMAGE

START = AF_IMAGE / "jupyter" / "start.sh"


def run_hooks_function():
    text = START.read_text()
    return re.search(r"^run-hooks\(\) \{.*?^\}$", text, re.M | re.S).group()


@pytest.fixture()
def run_hooks(tmp_path):
    """Run run-hooks over the given hook bodies, then `after`, under start.sh's `set -e`."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()

    def _run(after="", **bodies):
        for name, body in bodies.items():
            (hooks / f"{name}.sh").write_text(body)
        program = f'set -e\n{run_hooks_function()}\nrun-hooks "{hooks}"\n{after}\n'
        return subprocess.run(
            ["bash", "-c", program],
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"]},
        )

    return _run


def test_a_failed_step_does_not_stop_its_hook_or_the_next(run_hooks):
    result = run_hooks(
        a_failing="false\necho REST_OF_HOOK\n",
        b_next="echo NEXT_HOOK\n",
        after="echo SERVER_STARTS",
    )
    assert result.returncode == 0, result.stderr
    for line in ("REST_OF_HOOK", "NEXT_HOOK", "SERVER_STARTS"):
        assert line in result.stdout


def test_each_failed_step_reaches_the_log(run_hooks):
    """Also inside a function: most of what the hooks do runs in one."""
    result = run_hooks(
        a="false\nstep() {\n  ls /no/such/path\n  return 0\n}\nstep\n",
    )
    assert "a.sh:1: exit status 1: false" in result.stderr
    assert "a.sh:3: exit status" in result.stderr
    assert "ls /no/such/path" in result.stderr


def test_exports_of_a_failing_hook_reach_the_server(run_hooks):
    result = run_hooks(
        a="false\nexport FROM_HOOK=kept\n",
        after='echo "value=${FROM_HOOK}"',
    )
    assert "value=kept" in result.stdout


def test_a_guarded_step_stays_quiet(run_hooks):
    result = run_hooks(
        a='[ -e /no/such/path ] && echo no\nfalse || true\necho "done"\n'
    )
    assert "WARNING" not in result.stderr


def test_a_failure_after_the_hooks_still_ends_start_sh(run_hooks):
    """The refit and the exec around the hooks stay fatal: without them there is no session."""
    result = run_hooks(a="false\n", after="false\necho NOT_REACHED")
    assert result.returncode != 0
    assert "NOT_REACHED" not in result.stdout
