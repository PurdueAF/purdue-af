"""Tests for .github/workflows/validate-manifests.sh.

Only the retry wrapper is unit-tested here: the rest of the script needs
kustomize/flux/kubeconform/helm and runs in CI. `helm template` fetches every
chart over the network, so a reset connection to a chart host used to fail the
whole run for reasons unrelated to the manifests — while a genuinely broken
chart must still fail."""

import shutil
import subprocess
import textwrap

import pytest
from common import REPO

SCRIPT = REPO / ".github" / "workflows" / "validate-manifests.sh"

STUB_HELM = """#!/bin/bash
n=$(cat "$STUB_STATE" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$STUB_STATE"
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
        result = subprocess.run(
            [
                "bash",
                "-c",
                textwrap.dedent(f"""
                    source '{tmp_path}/fn.sh'
                    vals=()
                    helm_template_retry demo 1.0.0 vals chart --repo http://x
                """),
            ],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{bindir}:/usr/bin:/bin",
                "STUB_STATE": str(state),
                "STUB_FAIL_TIMES": str(fail_times),
                "KUBE_VERSION": "1.29.0",
                "HELM_RETRY_DELAY": "0",
            },
        )
        return result, int(state.read_text())

    return _run


def test_succeeds_without_retrying_when_helm_works(call_retry):
    result, calls = call_retry(0)
    assert result.returncode == 0
    assert calls == 1, "a working chart must not be fetched repeatedly"


def test_recovers_from_transient_chart_host_failures(call_retry):
    """The regression: two dropped connections used to fail the whole run."""
    result, calls = call_retry(2)
    assert result.returncode == 0
    assert calls == 3


def test_still_fails_when_every_attempt_fails(call_retry):
    """Retrying must not mask a genuinely broken chart or values file."""
    result, calls = call_retry(99)
    assert result.returncode != 0
    assert calls == 3, "attempts should be bounded"
    assert "connection reset by peer" in result.stderr, (
        "the last helm error must reach the log, or failures are undebuggable"
    )


def test_script_is_valid_bash():
    assert shutil.which("bash")
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
