"""Tests for docker/af-node-monitor pure helpers (no cluster required)."""

import json

import pytest
from conftest import job_runner

runner = job_runner()


# ── env handling ──────────────────────────────────────────────────────────────


def test_get_env_required_raises(monkeypatch):
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    with pytest.raises(RuntimeError, match="DOES_NOT_EXIST"):
        runner._get_env("DOES_NOT_EXIST", required=True)


def test_get_env_default():
    assert runner._get_env("DOES_NOT_EXIST_EITHER", "fallback") == "fallback"


# ── name sanitisation (used to build filesystem result paths) ─────────────────


def test_sanitized_mount_name():
    assert runner._sanitized_mount_name("/depot/") == "depot"
    assert runner._sanitized_mount_name("/work/users/") == "work_users"
    assert runner._sanitized_mount_name("cvmfs") == "cvmfs"
    assert runner._sanitized_mount_name("/") == "root"


def test_sanitized_node_name():
    assert runner._sanitized_node_name(" node-a.cluster ") == "node-a.cluster"
    assert runner._sanitized_node_name("bad/name") == "bad_name"
    assert runner._sanitized_node_name("") == ""


# ── result persistence ────────────────────────────────────────────────────────


def test_load_previous_result_missing_file(tmp_path):
    assert runner._load_previous_result(tmp_path / "none.json") == {}


def test_load_previous_result_corrupt_json(tmp_path):
    p = tmp_path / "result.json"
    p.write_text("{ not json")
    assert runner._load_previous_result(p) == {}


def test_write_result_atomic_roundtrip(tmp_path):
    p = tmp_path / "nested" / "result.json"
    runner._write_result_atomic(p, {"ok": True, "value": 1.5})

    assert json.loads(p.read_text()) == {"ok": True, "value": 1.5}
    assert not p.with_suffix(".json.tmp").exists()  # tmp file cleaned up


def test_write_result_atomic_overwrites(tmp_path):
    p = tmp_path / "result.json"
    runner._write_result_atomic(p, {"v": 1})
    runner._write_result_atomic(p, {"v": 2})
    assert runner._load_previous_result(p) == {"v": 2}


# ── subprocess wrapper ────────────────────────────────────────────────────────


def test_run_subprocess_success():
    ok, timeout, err = runner._run_subprocess(["true"], timeout_s=5)
    assert (ok, timeout, err) == (True, False, "")


def test_run_subprocess_failure_captures_stderr():
    ok, timeout, err = runner._run_subprocess(
        ["python3", "-c", "import sys; sys.exit('boom')"], timeout_s=5
    )
    assert ok is False
    assert timeout is False
    assert "boom" in err


def test_run_subprocess_timeout():
    ok, timeout, err = runner._run_subprocess(["sleep", "5"], timeout_s=0.2)
    assert (ok, timeout, err) == (False, True, "timeout")


def test_run_subprocess_missing_binary():
    ok, timeout, err = runner._run_subprocess(["definitely-not-a-binary"], timeout_s=5)
    assert ok is False
    assert err
