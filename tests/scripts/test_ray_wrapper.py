"""Tests for docker/purdue-af/ray-wrapper — the `ray` on a session's PATH.

It runs the Ray CLI through uv and, when AF_RAY_ENV names an environment,
tells the Ray Train gateway to start the user's cluster in it. uvx is replaced
by a stub that records its argv and the headers it would send.
"""

import json
import subprocess

import pytest
import yaml
from common import REPO, load_script

WRAPPER = REPO / "docker/purdue-af/ray-wrapper"
TEMPLATE = REPO / "apps/ray-train/raycluster.yaml"
gateway = load_script(REPO / "apps/ray-train/gateway.py", "ray_train_gateway_headers")

STUB = """#!/bin/bash
printf '%s\\n' "$*" > "$UVX_STUB_LOG"
printf '%s' "${RAY_JOB_HEADERS:-}" > "$UVX_STUB_LOG.headers"
"""


@pytest.fixture()
def run_ray(tmp_path):
    """Run the wrapper with uvx stubbed; returns (result, argv|None, headers|None)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uvx"
    stub.write_text(STUB)
    stub.chmod(0o755)
    log = tmp_path / "uvx.argv"

    def _run(*args, **env):
        for f in (log, log.with_name(log.name + ".headers")):
            f.unlink(missing_ok=True)
        result = subprocess.run(
            ["bash", str(WRAPPER), *args],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "HOME": str(tmp_path),
                "UVX_STUB_LOG": str(log),
                **env,
            },
        )
        if not log.exists():
            return result, None, None
        headers = log.with_name(log.name + ".headers").read_text()
        return result, log.read_text().strip(), json.loads(headers) if headers else {}

    return _run


def environment(prefix, ray="2.58.0"):
    site_packages = prefix / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    if ray:
        (site_packages / f"ray-{ray}.dist-info").mkdir()
    return prefix


def asks_for(prefix):
    return {gateway.ENV_HEADER: str(prefix.resolve())}


def test_the_cli_is_the_clusters_ray_version(run_ray):
    ray_version = yaml.safe_load(TEMPLATE.read_text())["spec"]["rayVersion"]
    result, argv, headers = run_ray("job", "submit", "--", "python", "train.py")
    assert result.returncode == 0
    assert (
        argv == f"--from ray[default]=={ray_version} ray job submit -- python train.py"
    )
    assert headers == {}


@pytest.mark.parametrize("manifest", ["pixi.toml", "pyproject.toml"])
def test_a_pixi_project_runs_its_default_environment(run_ray, tmp_path, manifest):
    project = tmp_path / "project"
    prefix = environment(project / ".pixi" / "envs" / "default")
    (project / manifest).write_text("")
    result, _, headers = run_ray("job", "submit", AF_RAY_ENV=str(project))
    assert result.returncode == 0, result.stderr
    assert headers == asks_for(prefix)


def test_an_environment_prefix_runs_as_it_is(run_ray, tmp_path):
    prefix = environment(tmp_path / "project" / ".pixi" / "envs" / "gpu")
    _, _, headers = run_ray("job", "submit", AF_RAY_ENV=str(prefix))
    assert headers == asks_for(prefix)


def test_the_cluster_is_given_the_physical_path(run_ray, tmp_path):
    """Sessions reach /work through ~/work, a symlink the cluster does not have."""
    prefix = environment(tmp_path / "work" / "env")
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "work").symlink_to(tmp_path / "work")
    _, _, headers = run_ray("job", "submit", AF_RAY_ENV=str(tmp_path / "home/work/env"))
    assert headers == asks_for(prefix)


def test_another_header_setting_gives_way(run_ray, tmp_path):
    prefix = environment(tmp_path / "env")
    _, _, headers = run_ray(
        "job", "submit", AF_RAY_ENV=str(prefix), RAY_JOB_HEADERS='{"X-Other": "1"}'
    )
    assert headers == asks_for(prefix)


def test_a_project_that_is_not_installed_is_refused(run_ray, tmp_path):
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "pixi.toml").write_text("")
    result, argv, _ = run_ray("job", "submit", AF_RAY_ENV=str(tmp_path / "project"))
    assert result.returncode == 1
    assert "pixi install" in result.stderr
    assert argv is None


def test_an_environment_without_ray_is_refused(run_ray, tmp_path):
    prefix = environment(tmp_path / "env", ray=None)
    result, argv, _ = run_ray("job", "submit", AF_RAY_ENV=str(prefix))
    assert result.returncode == 1
    assert "has no Ray" in result.stderr
    assert argv is None


@pytest.mark.parametrize(
    "ray, runs",
    [("2.52.0", False), ("1.13.1", False), ("2.53.0", True), ("3.0.0", True)],
)
def test_a_cluster_needs_a_ray_that_serves_kuberays_probe(run_ray, tmp_path, ray, runs):
    prefix = environment(tmp_path / "env", ray=ray)
    result, argv, _ = run_ray("job", "submit", AF_RAY_ENV=str(prefix))
    assert (result.returncode == 0) == runs, result.stderr
    assert (argv is not None) == runs


def test_a_path_that_would_break_the_header_is_refused(run_ray, tmp_path):
    prefix = environment(tmp_path / 'my "env"')
    result, argv, _ = run_ray("job", "submit", AF_RAY_ENV=str(prefix))
    assert result.returncode == 1
    assert argv is None
