"""Tests for the publish stage of .github/workflows/ci.yml.

Publish is the last gate before images reach the cluster, and it talks to GHCR
on every call. Both failure modes seen in practice — secondary rate limits on
back-to-back retags, and DNS/network blips fetching blobs — are transient, so
an unguarded call turns a fully green pipeline red. These tests assert every
GHCR call stays wrapped in the retry helper."""

import re

import yaml
from common import REPO

CI = REPO / ".github/workflows/ci.yml"


def publish_steps():
    doc = yaml.safe_load(CI.read_text())
    return [s for s in doc["jobs"]["publish"]["steps"] if "run" in s]


def test_every_imagetools_call_is_retried():
    """`digest=$(docker buildx imagetools inspect ...)` under `set -e` exits the
    step on the first blip, before any retry can run — that is exactly how run
    31053841647 failed."""
    unguarded = []
    for step in publish_steps():
        # join backslash continuations first: a wrapped call's second line
        # looks unguarded on its own
        joined = step["run"].replace("\\\n", " ")
        for line in joined.splitlines():
            stripped = line.strip()
            if "docker buildx imagetools" not in stripped:
                continue
            # the helper definitions themselves are the retry
            if stripped.startswith(("until ", "retag()", "ghcr_retry()")):
                continue
            if "ghcr_retry" in stripped or stripped.startswith("retag "):
                continue
            unguarded.append(f"{step.get('name')}: {stripped}")
    assert not unguarded, f"GHCR calls with no retry: {unguarded}"


def test_retry_progress_does_not_pollute_captured_output():
    """The digest is read from stdout; a warning printed there would be
    substituted into the image reference."""
    found = 0
    for step in publish_steps():
        # extract each ghcr_retry() function body (steps define their own copy)
        for body in re.findall(
            r"ghcr_retry\(\)\s*\{.*?\n\s*\}", step["run"], re.DOTALL
        ):
            found += 1
            for line in body.splitlines():
                if "::warning::" in line or "::error::" in line:
                    assert ">&2" in line, (
                        f"annotation not sent to stderr: {line.strip()}"
                    )
    assert found, "no ghcr_retry() definitions found in publish steps"


def test_retry_is_bounded():
    """An unbounded loop would hang the job instead of failing it."""
    run = next(s["run"] for s in publish_steps() if "ghcr_retry()" in s["run"])
    assert re.search(r"attempt=1 max=\d+", run)
    assert 'attempt" -ge "$max' in run
