from __future__ import annotations

import subprocess

import pytest


def test_check_if_directory_exists_reports_success_for_matching_checksum(
    monkeypatch, module_loader, prometheus_stub
) -> None:
    module = module_loader(
        "apps/monitoring/af-monitoring/node_healthcheck.py",
        extra_modules={"prometheus_client": prometheus_stub},
    )

    class FakeProc:
        returncode = 0

        def __init__(self) -> None:
            self.killed = False

        def communicate(self, timeout=None):
            return ("abc123  /tmp/validate.txt\n", "")

        def kill(self) -> None:
            self.killed = True

    proc = FakeProc()
    popen_calls = []

    def _fake_popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        return proc

    times = iter([100.0, 100.2])
    monkeypatch.setattr(module.time, "time", lambda: next(times))
    monkeypatch.setattr(module.subprocess, "Popen", _fake_popen)

    valid, elapsed_ms = module.check_if_directory_exists(("/tmp/validate.txt", "abc123"))

    assert valid is True
    assert elapsed_ms == pytest.approx(200.0)
    assert popen_calls[0][0] == ["/usr/bin/md5sum", "/tmp/validate.txt"]


def test_check_if_directory_exists_returns_timeout_result(
    monkeypatch, module_loader, prometheus_stub
) -> None:
    module = module_loader(
        "apps/monitoring/af-monitoring/node_healthcheck.py",
        extra_modules={"prometheus_client": prometheus_stub},
    )

    class FakeProc:
        returncode = 0

        def __init__(self) -> None:
            self.killed = False
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(cmd="md5sum", timeout=timeout)
            return ("", "")

        def kill(self) -> None:
            self.killed = True

    proc = FakeProc()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: proc)

    valid, elapsed_ms = module.check_if_directory_exists(("/tmp/validate.txt", "abc123"))

    assert valid is False
    assert elapsed_ms == 3000
    assert proc.killed is True


def test_update_metrics_writes_mount_health_and_ping(
    monkeypatch, module_loader, prometheus_stub, recording_gauge_cls
) -> None:
    module = module_loader(
        "apps/monitoring/af-monitoring/node_healthcheck.py",
        extra_modules={"prometheus_client": prometheus_stub},
    )
    module.mount_valid = recording_gauge_cls()
    module.mount_ping_ms = recording_gauge_cls()
    module.mounts = {
        "mount-a": ("/mnt/a", "sum-a"),
        "mount-b": ("/mnt/b", "sum-b"),
    }
    responses = iter([(True, 12.5), (False, 22.5)])
    monkeypatch.setattr(
        module,
        "check_if_directory_exists",
        lambda _path_tuple: next(responses),
    )

    module.update_metrics()

    key_a = (("mount_name", "mount-a"), ("mount_path", "/mnt/a"))
    key_b = (("mount_name", "mount-b"), ("mount_path", "/mnt/b"))
    assert module.mount_valid.label_children[key_a].values == [1]
    assert module.mount_valid.label_children[key_b].values == [0]
    assert module.mount_ping_ms.label_children[key_a].values == [12.5]
    assert module.mount_ping_ms.label_children[key_b].values == [22.5]
