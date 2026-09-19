"""The pixi CLI version has one pin, ARG PIXI_VERSION in the AF Dockerfile:
the pixi workflows read it at run time and Renovate's copy is asserted equal.
A lock written by a newer pixi (format v7) is unreadable by the pinned one."""

import os
import re
import subprocess

import yaml
from common import REPO

DOCKERFILE = REPO / "docker/purdue-af/Dockerfile"
RENOVATE = REPO / ".github/renovate.json5"
WORKFLOWS = [
    REPO / f".github/workflows/ci-pixi-{env}.yml" for env in ("base", "global")
]


def pinned() -> str:
    (version,) = re.findall(
        r'^ARG PIXI_VERSION="([^"]+)"', DOCKERFILE.read_text(), re.M
    )
    return version


def test_renovate_constraint_equals_the_dockerfile_pin():
    (constraint,) = re.findall(r"pixi:\s*'([^']+)'", RENOVATE.read_text())
    assert constraint == pinned()


def test_workflows_read_the_pin_instead_of_carrying_one(tmp_path):
    for workflow in WORKFLOWS:
        text = workflow.read_text()
        assert not re.search(r"pixi-version:\s*v?\d", text), workflow.name
        doc = yaml.safe_load(text)
        steps = [s for job in doc["jobs"].values() for s in job["steps"]]
        (reader,) = [s for s in steps if s.get("id") == "pixi"]
        assert "docker/purdue-af/Dockerfile" in reader["run"]
        (setup,) = [s for s in steps if "setup-pixi" in s.get("uses", "")]
        assert setup["with"]["pixi-version"] == "v${{ steps.pixi.outputs.version }}"
        assert setup["if"] == reader["if"]
        # the step's own shell yields the pin, byte for byte
        output = tmp_path / workflow.name
        subprocess.run(
            ["bash", "-euo", "pipefail", "-c", reader["run"]],
            cwd=REPO,
            env={**os.environ, "GITHUB_OUTPUT": str(output)},
            capture_output=True,
            text=True,
            check=True,
        )
        assert output.read_text().strip() == f"version={pinned()}"
