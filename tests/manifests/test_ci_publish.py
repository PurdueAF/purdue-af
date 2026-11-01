"""Tests for the GHCR retry helper and the workflows that call it."""

import subprocess

import yaml
from common import REPO

HELPER = REPO / ".github/workflows/ghcr.sh"
WORKFLOWS = [".github/workflows/ci.yml", ".github/workflows/release-image.yml"]


def run_steps():
    for workflow in WORKFLOWS:
        doc = yaml.safe_load((REPO / workflow).read_text())
        for job in doc["jobs"].values():
            yield from (s for s in job.get("steps", []) if "run" in s)


def retry(body: str, delay: int = 0) -> subprocess.CompletedProcess[str]:
    script = f'source "{HELPER}"\nflaky() {{ {body}; }}\nghcr_retry probe flaky'
    return subprocess.run(
        ["bash", "-c", script],
        env={"GHCR_RETRY_DELAY": str(delay), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


def test_every_captured_imagetools_call_is_retried():
    """`digest=$(docker buildx imagetools inspect ...)` under `set -e` exits the
    step on the first blip, before any retry can run."""
    unguarded = []
    for step in run_steps():
        # join backslash continuations first: a wrapped call's second line looks unguarded
        for line in step["run"].replace("\\\n", " ").splitlines():
            stripped = line.strip()
            if "docker buildx imagetools" not in stripped or "ghcr_retry" in stripped:
                continue
            # `if` takes the other branch and `|| true` fails safe, so neither exits the step
            if not (stripped.startswith("if ") or stripped.endswith("|| true")):
                unguarded.append(stripped)
    assert not unguarded


def test_retry_returns_the_commands_stdout_alone():
    """The digest is read from stdout; a warning printed there would be
    substituted into the image reference."""
    result = retry('n=$(( ${n:-0} + 1 )); [ "$n" -gt 1 ] || return 1; echo sha256:abc')
    assert result.returncode == 0
    assert result.stdout == "sha256:abc\n"
    assert "retrying" in result.stderr


def test_retry_is_bounded():
    """An unbounded loop would hang the job instead of failing it."""
    result = retry("echo tried; false")
    assert result.returncode != 0
    assert result.stdout.count("tried") == 5
    assert "failed after 5 attempts" in result.stderr
