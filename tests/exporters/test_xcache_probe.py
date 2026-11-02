"""Tests for docker/af-node-monitor/xcache_probe.py.

A read that ran is a verdict on XCache. An image without the client is not, so
it must leave the row blank rather than paint it red.
"""

import subprocess
import types

import pytest
import xcache_probe as xp
from prometheus_client import REGISTRY

URL = "root://xcache.test//store/file.root"


def _value():
    return REGISTRY.get_sample_value("af_xcache_read_ok")


def _run_returning(code, stderr=""):
    def run(cmd, **kwargs):
        assert cmd == ["xrdcp", "--force", "--nopbar", URL, "/dev/null"]
        assert kwargs["timeout"] == xp.READ_TIMEOUT_S
        return types.SimpleNamespace(returncode=code, stderr=stderr)

    return run


def _run_raising(error):
    def run(cmd, **kwargs):
        raise error

    return run


@pytest.fixture(autouse=True)
def url(monkeypatch):
    monkeypatch.setattr(xp, "XCACHE_PROBE_URL", URL)


def test_a_completed_read_is_a_pass(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _run_returning(0))
    xp.probe_once()
    assert _value() == 1


def test_a_failed_read_is_a_fail_and_logs_the_reason(monkeypatch, capsys):
    stderr = "noise\n[FATAL] Auth failed\n"
    monkeypatch.setattr(subprocess, "run", _run_returning(54, stderr))
    xp.probe_once()
    assert _value() == 0
    assert "[FATAL] Auth failed" in capsys.readouterr().out


def test_a_read_that_never_ends_is_a_fail(monkeypatch):
    timeout = subprocess.TimeoutExpired("xrdcp", xp.READ_TIMEOUT_S)
    monkeypatch.setattr(subprocess, "run", _run_raising(timeout))
    xp.probe_once()
    assert _value() == 0


def test_no_client_leaves_the_last_verdict_alone(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _run_returning(0))
    xp.probe_once()
    monkeypatch.setattr(subprocess, "run", _run_raising(FileNotFoundError("xrdcp")))
    xp.probe_once()
    assert _value() == 1


def test_main_exits_when_its_code_changes(monkeypatch, tmp_path):
    code = tmp_path / "xcache_probe.py"
    code.write_text("old")
    monkeypatch.setattr(xp, "CODE", code)
    monkeypatch.setattr(xp, "start_http_server", lambda port: None)
    monkeypatch.setattr(xp, "probe_once", lambda: code.write_text("new"))
    monkeypatch.setattr(xp, "CHECK_INTERVAL_S", 0)
    xp.main()
