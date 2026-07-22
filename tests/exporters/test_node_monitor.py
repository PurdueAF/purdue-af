"""Tests for docker/af-node-monitor pure helpers (no cluster required)."""

import json

import pytest
from exporter_helpers import job_runner

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


# ── check functions (subprocess mocked) ───────────────────────────────────────


def stub_run_subprocess(monkeypatch, ok=True, timeout=False, reason=""):
    calls = []

    def fake(cmd, timeout_s):
        calls.append(cmd)
        return ok, timeout, reason

    monkeypatch.setattr(runner, "_run_subprocess", fake)
    return calls


def test_check_ping_without_checksum(monkeypatch):
    monkeypatch.setattr(runner, "CHECKSUM", "")
    calls = stub_run_subprocess(monkeypatch)

    ok, timeout, elapsed = runner._check_ping()

    assert (ok, timeout) == (True, False)
    assert elapsed >= 0
    assert calls[0][0] == "cat"  # no checksum: plain read


def test_check_ping_failure(monkeypatch):
    monkeypatch.setattr(runner, "CHECKSUM", "")
    stub_run_subprocess(monkeypatch, ok=False, reason="io error")

    ok, timeout, _ = runner._check_ping()
    assert (ok, timeout) == (False, False)


def test_check_ping_timeout(monkeypatch):
    monkeypatch.setattr(runner, "CHECKSUM", "")
    stub_run_subprocess(monkeypatch, ok=False, timeout=True)

    ok, timeout, _ = runner._check_ping()
    assert (ok, timeout) == (False, True)


def test_check_ping_checksum_match(monkeypatch):
    import types

    monkeypatch.setattr(runner, "CHECKSUM", "abc123")
    stub_run_subprocess(monkeypatch)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **kw: types.SimpleNamespace(stdout="abc123  /file\n"),
    )

    ok, timeout, _ = runner._check_ping()
    assert (ok, timeout) == (True, False)


def test_check_ping_checksum_mismatch_means_corruption(monkeypatch):
    import types

    monkeypatch.setattr(runner, "CHECKSUM", "abc123")
    stub_run_subprocess(monkeypatch)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **kw: types.SimpleNamespace(stdout="DIFFERENT  /file\n"),
    )

    ok, timeout, _ = runner._check_ping()
    assert (ok, timeout) == (False, False)


def test_check_metadata_skipped_without_dir(monkeypatch):
    monkeypatch.setattr(runner, "METADATA_DIR", None)
    assert runner._check_metadata() == (True, False, None)


def test_check_metadata_lists_dir(monkeypatch):
    monkeypatch.setattr(runner, "METADATA_DIR", "/data")
    calls = stub_run_subprocess(monkeypatch)

    ok, timeout, elapsed = runner._check_metadata()

    assert (ok, timeout) == (True, False)
    assert calls[0][:2] == ["ls", "-la"]


def test_check_throughput_disabled(monkeypatch):
    monkeypatch.setattr(runner, "ENABLE_FIO", False)
    assert runner._check_throughput(None) == (True, False, None, None)


def test_check_throughput_respects_interval(monkeypatch):
    import time

    monkeypatch.setattr(runner, "ENABLE_FIO", True)
    monkeypatch.setattr(runner, "FIO_FILE", "/probe")
    monkeypatch.setattr(runner, "FIO_INTERVAL_S", 1800.0)

    recent = time.time() - 10
    ok, timeout, gbps, ts = runner._check_throughput(recent)
    assert (ok, timeout, gbps, ts) == (True, False, None, recent)  # skipped


def test_check_throughput_parses_fio_json(monkeypatch):
    import json as _json
    import types

    monkeypatch.setattr(runner, "ENABLE_FIO", True)
    monkeypatch.setattr(runner, "FIO_FILE", "/probe")
    fio_out = _json.dumps({"jobs": [{"read": {"bw_bytes": 2_500_000_000}}]})
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **kw: types.SimpleNamespace(returncode=0, stdout=fio_out),
    )

    ok, timeout, gbps, ts = runner._check_throughput(None)

    assert (ok, timeout) == (True, False)
    assert gbps == 2.5
    assert ts is not None


def test_check_throughput_fio_failure(monkeypatch):
    import types

    monkeypatch.setattr(runner, "ENABLE_FIO", True)
    monkeypatch.setattr(runner, "FIO_FILE", "/probe")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **kw: types.SimpleNamespace(returncode=1, stdout=""),
    )

    ok, timeout, gbps, ts = runner._check_throughput(None)
    assert (ok, gbps) == (False, 0.0)
    assert ts is None  # timestamp not advanced on failure


def test_check_throughput_fio_timeout(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(runner, "ENABLE_FIO", True)
    monkeypatch.setattr(runner, "FIO_FILE", "/probe")

    def boom(*a, **kw):
        raise sp.TimeoutExpired(cmd="fio", timeout=1)

    monkeypatch.setattr(runner.subprocess, "run", boom)
    ok, timeout, gbps, ts = runner._check_throughput(None)
    assert (ok, timeout, gbps) == (False, True, 0.0)
    assert ts is None


def test_check_throughput_fio_bad_json(monkeypatch):
    import types

    monkeypatch.setattr(runner, "ENABLE_FIO", True)
    monkeypatch.setattr(runner, "FIO_FILE", "/probe")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **kw: types.SimpleNamespace(returncode=0, stdout="not-json"),
    )
    ok, timeout, gbps, ts = runner._check_throughput(None)
    assert (ok, timeout, gbps) == (False, False, 0.0)
    assert ts is None


def test_check_ping_failure_without_reason(monkeypatch):
    monkeypatch.setattr(runner, "CHECKSUM", "")
    stub_run_subprocess(monkeypatch, ok=False, reason="")
    ok, timeout, _ = runner._check_ping()
    assert (ok, timeout) == (False, False)


def test_check_metadata_failure_paths(monkeypatch):
    monkeypatch.setattr(runner, "METADATA_DIR", "/data")
    stub_run_subprocess(monkeypatch, ok=False, timeout=True)
    ok, timeout, _ = runner._check_metadata()
    assert (ok, timeout) == (False, True)

    stub_run_subprocess(monkeypatch, ok=False, reason="")
    ok, timeout, _ = runner._check_metadata()
    assert (ok, timeout) == (False, False)


# ── main orchestration ────────────────────────────────────────────────────────


def test_main_writes_timeout_when_ping_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runner, "MOUNT_NAME", "/depot/")
    monkeypatch.setattr(runner, "NODE_NAME", "node-a")
    monkeypatch.setattr(runner, "ENABLE_FIO", False)
    monkeypatch.setattr(runner, "FIO_FILE", None)
    monkeypatch.setattr(runner, "_check_ping", lambda: (False, False, 12.0))
    monkeypatch.setattr(runner, "_check_metadata", lambda: (True, False, 1.0))

    runner.main()

    result = json.loads((tmp_path / "depot__node-a.json").read_text())
    assert result["ok"] is False
    assert result["timeout"] is True
    assert result["throughput_gbps"] == 0.0
    assert result["node"] == "node-a"


def test_main_success_without_node_name(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runner, "MOUNT_NAME", "/work/")
    monkeypatch.setattr(runner, "NODE_NAME", "")
    monkeypatch.setattr(runner, "ENABLE_FIO", False)
    monkeypatch.setattr(runner, "FIO_FILE", None)
    monkeypatch.setattr(runner, "_check_ping", lambda: (True, False, 5.0))
    monkeypatch.setattr(runner, "_check_metadata", lambda: (True, False, 2.0))
    monkeypatch.setattr(
        runner, "_check_throughput", lambda last: (True, False, None, last)
    )

    runner.main()

    result = json.loads((tmp_path / "work.json").read_text())
    assert result["ok"] is True
    assert result["timeout"] is False
    assert result["ping_ms"] == 5.0
    assert result["node"] == ""


def test_main_runs_fio_and_records_throughput(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runner, "MOUNT_NAME", "/depot/")
    monkeypatch.setattr(runner, "NODE_NAME", "n1")
    monkeypatch.setattr(runner, "ENABLE_FIO", True)
    monkeypatch.setattr(runner, "FIO_FILE", "/probe")
    monkeypatch.setattr(runner, "FIO_INTERVAL_S", 1.0)
    monkeypatch.setattr(runner, "_check_ping", lambda: (True, False, 1.0))
    monkeypatch.setattr(runner, "_check_metadata", lambda: (True, False, 1.0))
    monkeypatch.setattr(
        runner, "_check_throughput", lambda last: (True, False, 3.5, 12345.0)
    )

    runner.main()

    result = json.loads((tmp_path / "depot__n1.json").read_text())
    assert result["ok"] is True
    assert result["throughput_gbps"] == 3.5
    assert result["last_fio_ts"] == 12345.0


def test_main_tolerates_corrupt_previous_last_fio_ts(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runner, "MOUNT_NAME", "/depot/")
    monkeypatch.setattr(runner, "NODE_NAME", "n1")
    path = tmp_path / "depot__n1.json"
    path.write_text('{"last_fio_ts": "not-a-float", "throughput_gbps": 1.0}')
    monkeypatch.setattr(runner, "ENABLE_FIO", False)
    monkeypatch.setattr(runner, "FIO_FILE", None)
    monkeypatch.setattr(runner, "_check_ping", lambda: (True, False, 1.0))
    monkeypatch.setattr(runner, "_check_metadata", lambda: (True, False, None))
    monkeypatch.setattr(
        runner, "_check_throughput", lambda last: (True, False, None, last)
    )

    runner.main()

    result = json.loads(path.read_text())
    assert result["ok"] is True
    assert result["last_fio_ts"] is None
