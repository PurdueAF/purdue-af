"""The lock-commit path of the pixi workflows, against a local origin."""

import shutil
import subprocess

import pytest
from common import REPO

SCRIPTS = ["commit-lockfile.sh", "retrigger-after-lock-push.sh"]


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "pixi/base").mkdir(parents=True)
    (work / "pixi/base/pixi.lock").write_text("old\n")
    (work / ".github/workflows").mkdir(parents=True)
    for script in SCRIPTS:
        shutil.copy(REPO / ".github/workflows" / script, work / ".github/workflows")
    git(work, "config", "user.name", "t")
    git(work, "config", "user.email", "t@example.com")
    git(work, "add", ".")
    git(work, "commit", "-qm", "init")
    git(work, "push", "-q", "origin", "HEAD:main")
    return work


def commit_lock(work, tmp_path, head_repo=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\necho "$*" >> "$GH_LOG"\necho 123\n')
    gh.chmod(0o755)
    return subprocess.run(
        [".github/workflows/commit-lockfile.sh", "base"],
        cwd=work,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin:/opt/homebrew/bin",
            "HOME": str(tmp_path),
            "BRANCH": "main",
            "HEAD_REPO": head_repo,
            "GITHUB_REPOSITORY": "PurdueAF/purdue-af",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "GH_LOG": str(tmp_path / "gh.log"),
        },
        capture_output=True,
        text=True,
    )


def test_lock_commit_reaches_the_branch_and_stops_this_run(clone, tmp_path):
    (clone / "pixi/base/pixi.lock").write_text("new\n")
    result = commit_lock(clone, tmp_path)
    assert result.returncode == 1, result.stderr
    assert git(clone, "log", "-1", "--format=%s", "origin/main") == (
        "pixi-base: regenerate the lockfile"
    )
    assert git(clone, "log", "-1", "--format=%ae", "origin/main").startswith(
        "41898282+github-actions[bot]"
    )
    assert "actions/runs/123/approve" in (tmp_path / "gh.log").read_text()


def test_unchanged_lock_commits_nothing(clone, tmp_path):
    before = git(clone, "rev-parse", "HEAD")
    assert commit_lock(clone, tmp_path).returncode == 0
    assert git(clone, "rev-parse", "origin/main") == before


def test_a_fork_pr_gets_the_command_instead_of_a_commit(clone, tmp_path):
    (clone / "pixi/base/pixi.lock").write_text("new\n")
    before = git(clone, "rev-parse", "origin/main")
    result = commit_lock(clone, tmp_path, head_repo="someone/fork")
    assert result.returncode == 1
    assert "pixi lock --manifest-path pixi/base/pixi.toml" in result.stdout
    assert git(clone, "rev-parse", "origin/main") == before
