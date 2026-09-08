"""Tests for docker/purdue-af/scripts/agent-wrapper.sh.

The wrapper sits in front of every agent CLI in the image, on the path a user
is waiting on. So the properties worth pinning down are mostly about it being
invisible: the agent's arguments, streams and exit status must survive it
untouched, and nothing it does for accounting may be able to stop an agent
from starting.
"""

import json
import os
import signal
import subprocess

import pytest
from common import REPO

WRAPPER = REPO / "docker" / "purdue-af" / "scripts" / "agent-wrapper.sh"

REAL_AGENT = """#!/bin/bash
echo "stdout: $*"
echo "stderr: $*" >&2
exit ${AGENT_EXIT:-0}
"""


@pytest.fixture()
def wrapper(tmp_path):
    """A sandbox that looks like the image: a prefix with bin/ and versions.env,
    and the wrapper symlinked under each agent's name."""
    prefix = tmp_path / "npm-global"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "versions.env").write_text(
        "PAF_VERSION_claude=2.1.263\nPAF_VERSION_codex=0.153.4\n"
    )
    sink = tmp_path / "usage.log"
    sink.write_text("")

    def _run(agent="claude", args=(), install=True, agent_exit=0, **env):
        if install:
            real = prefix / "bin" / agent
            real.write_text(REAL_AGENT)
            real.chmod(0o755)
        link = tmp_path / agent
        if not link.exists():
            link.symlink_to(WRAPPER)
        result = subprocess.run(
            ["bash", str(link), *args],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "NPM_GLOBAL": str(prefix),
                "PAF_USAGE_SINK": str(sink),
                "NB_USER": "jovyan",
                "AGENT_EXIT": str(agent_exit),
                **env,
            },
        )
        records = [json.loads(ln) for ln in sink.read_text().splitlines() if ln.strip()]
        return result, records

    return _run


def test_the_agent_runs_with_its_arguments_and_streams_intact(wrapper):
    result, _ = wrapper(args=["--help", "-x", "a b"])
    assert result.returncode == 0
    assert result.stdout == "stdout: --help -x a b\n"
    assert result.stderr == "stderr: --help -x a b\n"


def test_accounting_never_reaches_the_users_terminal(wrapper):
    """The records go to the container's stdout, which is a different stream
    from the one the agent's TUI owns."""
    result, records = wrapper()
    assert "agent_run" not in result.stdout
    assert "agent_run" not in result.stderr
    assert len(records) == 2


def test_the_exit_code_is_the_agents_own(wrapper):
    result, records = wrapper(agent_exit=42)
    assert result.returncode == 42
    assert records[-1]["exit_code"] == 42


def test_records_carry_agent_version_and_user(wrapper):
    _, records = wrapper(agent="codex")
    start, stop = records
    assert start == {
        "event": "agent_run",
        "phase": "start",
        "agent": "codex",
        "version": "0.153.4",
        "user": "jovyan",
    }
    assert stop["phase"] == "stop"
    assert stop["duration_ms"] >= 0


def test_unknown_version_when_the_manifest_is_missing(wrapper, tmp_path):
    (tmp_path / "npm-global" / "versions.env").unlink()
    _, records = wrapper()
    assert records[0]["version"] == "unknown"


def test_the_agent_gets_its_own_otel_identity(wrapper, tmp_path):
    """Set here rather than pod-wide so jupyter-server's own tracer keeps its
    service name, and so agent telemetry carries the AF username."""
    real = tmp_path / "npm-global" / "bin" / "claude"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(
        '#!/bin/bash\necho "$OTEL_SERVICE_NAME|$OTEL_RESOURCE_ATTRIBUTES"\n'
    )
    real.chmod(0o755)
    result, _ = wrapper(install=False)
    service, attrs = result.stdout.strip().split("|")
    assert service == "claude"
    assert "user=jovyan" in attrs
    assert "af.agent=claude" in attrs


def test_existing_resource_attributes_are_kept(wrapper, tmp_path):
    real = tmp_path / "npm-global" / "bin" / "claude"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text('#!/bin/bash\necho "$OTEL_RESOURCE_ATTRIBUTES"\n')
    real.chmod(0o755)
    result, _ = wrapper(install=False, OTEL_RESOURCE_ATTRIBUTES="user=jovyan,team=cms")
    assert result.stdout.strip().startswith("user=jovyan,team=cms,")
    assert "af.facility=purdue-af" in result.stdout


def test_an_unwritable_sink_does_not_stop_the_agent(wrapper):
    """Accounting must never be the reason an agent fails to start."""
    result, _ = wrapper(PAF_USAGE_SINK="/nonexistent/dir/usage.log")
    assert result.returncode == 0
    assert result.stdout.startswith("stdout:")


def test_a_missing_agent_reports_clearly(wrapper):
    result, records = wrapper(install=False)
    assert result.returncode == 127
    assert "not installed" in result.stderr
    assert records == []


def test_a_username_cannot_break_the_json(wrapper):
    _, records = wrapper(NB_USER='ev"il\\')
    assert records[0]["user"] == "evil"


def test_the_wrapper_is_named_after_every_agent_in_the_image():
    """The wrapper dispatches on its own basename, so the Dockerfile symlinks
    and the versions manifest have to agree with the agents installed."""
    dockerfile = (REPO / "docker" / "purdue-af" / "Dockerfile").read_text()
    for agent in ("claude", "codex", "opencode"):
        assert f"PAF_VERSION_{agent}" in dockerfile
    assert "for agent in claude codex opencode" in dockerfile
    # /usr/local/bin must come before the real binaries for the links to win.
    path_line = next(
        ln for ln in dockerfile.splitlines() if ln.startswith('ENV PATH="')
    )
    assert path_line.index("/usr/local/bin") < path_line.index("/opt/npm-global/bin")


@pytest.mark.skipif(os.name != "posix", reason="signal semantics are POSIX")
def test_the_agent_still_receives_an_interrupt(wrapper, tmp_path):
    """`trap ':'` (not `trap ''`) is deliberate, and the difference is not
    cosmetic: a signal *ignored* by the shell is inherited as ignored by its
    children, which would leave the agent itself deaf to Ctrl-C. This runs the
    real thing — SIGINT to the wrapper's process group, then check the agent
    saw it."""
    real = tmp_path / "npm-global" / "bin" / "claude"
    real.parent.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "agent-was-interrupted"
    real.write_text(
        "#!/bin/bash\n"
        f"trap 'touch {marker}; exit 130' INT\n"
        "echo ready\n"
        "for _ in $(seq 100); do sleep 0.05; done\n"
    )
    real.chmod(0o755)
    link = tmp_path / "claude"
    if not link.exists():
        link.symlink_to(WRAPPER)

    proc = subprocess.Popen(
        ["bash", str(link)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={
            "PATH": "/usr/bin:/bin",
            "NPM_GLOBAL": str(tmp_path / "npm-global"),
            "PAF_USAGE_SINK": str(tmp_path / "usage.log"),
            "NB_USER": "jovyan",
        },
    )
    assert proc.stdout.readline().strip() == "ready"
    # Exactly what a terminal does with Ctrl-C: signal the whole group.
    os.killpg(proc.pid, signal.SIGINT)
    proc.wait(timeout=15)

    assert marker.exists(), "the agent did not receive SIGINT"
    # And the wrapper outlived it long enough to close the record.
    records = [
        json.loads(ln)
        for ln in (tmp_path / "usage.log").read_text().splitlines()
        if ln.strip()
    ]
    assert [r["phase"] for r in records] == ["start", "stop"]
    assert records[-1]["exit_code"] == 130
