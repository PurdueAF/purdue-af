"""Tests for .github/workflows/validate-manifests.sh.

Only the retry wrapper is unit-tested here: the rest of the script needs
kustomize/flux/kubeconform/helm and runs in CI. A transient chart-host failure
is retried; a genuinely broken chart still fails; the values reach helm."""

import shutil
import subprocess
import textwrap

import pytest
from common import REPO

SCRIPT = REPO / ".github" / "workflows" / "validate-manifests.sh"

STUB_HELM = """#!/bin/bash
n=$(cat "$STUB_STATE" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$STUB_STATE"
printf '%s\\n' "$@" > "$STUB_ARGV"
if [ "$n" -le "${STUB_FAIL_TIMES:-0}" ]; then
    echo "Error: read: connection reset by peer" >&2
    exit 1
fi
echo "rendered"
"""


@pytest.fixture()
def call_retry(tmp_path):
    """Run helm_template_retry with a stub helm that fails N times first."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "helm"
    stub.write_text(STUB_HELM)
    stub.chmod(0o755)

    # lift just the retry helper out of the script — sourcing the whole file
    # would run the real validation
    text = SCRIPT.read_text()
    start = text.index("HELM_ATTEMPTS=")
    end = text.index("\n}\n", text.index("helm_template_retry() {")) + 3
    (tmp_path / "fn.sh").write_text(text[start:end])

    def _run(fail_times):
        state = tmp_path / "state"
        state.write_text("0")
        argv = tmp_path / "argv"
        result = subprocess.run(
            [
                "bash",
                "-c",
                textwrap.dedent(f"""
                    source '{tmp_path}/fn.sh'
                    helm_template_retry demo 1.0.0 chart --repo http://x -f a.yaml -f 'b c.yaml'
                """),
            ],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{bindir}:/usr/bin:/bin",
                "STUB_STATE": str(state),
                "STUB_ARGV": str(argv),
                "STUB_FAIL_TIMES": str(fail_times),
                "KUBE_VERSION": "1.29.0",
                "HELM_RETRY_DELAY": "0",
            },
        )
        return result, int(state.read_text()), argv.read_text().splitlines()

    return _run


def test_succeeds_without_retrying_when_helm_works(call_retry):
    result, calls, _ = call_retry(0)
    assert result.returncode == 0
    assert calls == 1, "a working chart must not be fetched repeatedly"


def test_hands_every_values_file_to_helm(call_retry):
    """A chart rendered without its values validates only its defaults."""
    result, _, argv = call_retry(0)
    assert result.returncode == 0, result.stderr
    values = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-f"]
    assert values == ["a.yaml", "b c.yaml"], argv


def test_recovers_from_transient_chart_host_failures(call_retry):
    """Two dropped connections do not fail the run."""
    result, calls, _ = call_retry(2)
    assert result.returncode == 0
    assert calls == 3


def test_still_fails_when_every_attempt_fails(call_retry):
    """Retrying must not mask a genuinely broken chart or values file."""
    result, calls, _ = call_retry(99)
    assert result.returncode != 0
    assert calls == 3, "attempts should be bounded"
    assert "connection reset by peer" in result.stderr, (
        "the last helm error must reach the log, or failures are undebuggable"
    )


def test_script_is_valid_bash():
    assert shutil.which("bash")
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
