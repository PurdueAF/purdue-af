"""Tests for .github/workflows/validate-manifests.sh.

The retry wrapper, the source fetch and the render step are unit-tested here:
the rest of the script needs kustomize/flux/kubeconform/helm and runs in CI. A
transient chart-host failure is retried; a genuinely broken chart still fails;
the values reach helm; a failed fetch leaves nothing behind; a root that does
not render fails the run."""

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


def lift_retry():
    text = SCRIPT.read_text()
    start = text.index("HELM_ATTEMPTS=")
    end = text.index("\n}\n", text.index("fetch_ref() {")) + 3
    return text[start:end]


@pytest.fixture()
def call_retry(tmp_path):
    """Run helm_template_retry with a stub helm that fails N times first."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "helm"
    stub.write_text(STUB_HELM)
    stub.chmod(0o755)

    # sourcing the whole file would run the real validation
    (tmp_path / "fn.sh").write_text(lift_retry())

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


@pytest.mark.parametrize(
    "step, status",
    [(None, 0), ("kustomize", 1), ("flux", 1), ("yq", 1)],
)
def test_a_root_that_does_not_render_fails(tmp_path, step, status):
    """The script calls render_env in an `if`, which turns errexit off inside it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("kustomize", "flux", "yq"):
        stub = bindir / tool
        stub.write_text(f"#!/bin/bash\nexit {1 if tool == step else 0}\n")
        stub.chmod(0o755)

    text = SCRIPT.read_text()
    start = text.index("SUBST_DISABLED=")
    end = text.index("\n}\n", text.index("render_env() {")) + 3
    (tmp_path / "fn.sh").write_text(text[start:end])

    result = subprocess.run(
        [
            "bash",
            "-c",
            textwrap.dedent(f"""
                set -euo pipefail
                workdir='{tmp_path}'
                source '{tmp_path}/fn.sh'
                if ! render_env flux/demo/ >/dev/null; then exit 1; fi
            """),
        ],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
    )
    assert result.returncode == status, result.stderr


@pytest.fixture()
def source_repo(tmp_path):
    """A repository with a branch ahead of the commit that its tag names."""

    def git(*args):
        return subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                *args,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    repo = tmp_path / "src"
    repo.mkdir()
    git("init", "--quiet", "--initial-branch=main")
    (repo / "marker").write_text("first")
    git("add", "marker")
    git("commit", "--quiet", "-m", "first")
    git("tag", "v1")
    first = git("rev-parse", "HEAD")
    (repo / "marker").write_text("second")
    git("commit", "--quiet", "-am", "second")
    return repo, first


def fetch(tmp_path, url, ref):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (tmp_path / "fn.sh").write_text(lift_retry())
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"workdir='{work}'; source '{tmp_path}/fn.sh'; fetch_ref '{url}' '{ref}' '{work}/dest'",
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HELM_RETRY_DELAY": "0"},
    )
    return result, work


@pytest.mark.parametrize(
    "ref, marker", [("main", "second"), ("HEAD", "second"), ("v1", "first")]
)
def test_fetches_a_named_ref(tmp_path, source_repo, ref, marker):
    repo, _ = source_repo
    result, work = fetch(tmp_path, f"file://{repo}", ref)
    assert result.returncode == 0, result.stderr
    assert (work / "dest" / "marker").read_text() == marker


def test_fetches_a_commit(tmp_path, source_repo):
    repo, first = source_repo
    subprocess.run(
        ["git", "-C", str(repo), "config", "uploadpack.allowAnySHA1InWant", "true"],
        check=True,
    )
    result, work = fetch(tmp_path, f"file://{repo}", first)
    assert result.returncode == 0, result.stderr
    assert (work / "dest" / "marker").read_text() == "first"


def test_a_failed_fetch_leaves_nothing_behind(tmp_path, source_repo):
    """An existing clone directory reads as a finished clone."""
    repo, _ = source_repo
    result, work = fetch(tmp_path, f"file://{repo}", "no-such-ref")
    assert result.returncode != 0
    assert result.stderr.count("retrying") == 2, result.stderr
    assert list(work.iterdir()) == []
