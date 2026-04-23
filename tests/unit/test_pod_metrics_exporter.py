from __future__ import annotations

import glob
import os
from types import SimpleNamespace


def _load_exporter(monkeypatch, module_loader, prometheus_stub):
    monkeypatch.setattr(os, "listdir", lambda _path: ["jovyan", "slurm", "alice"])
    monkeypatch.setattr(glob, "glob", lambda _pattern: ["/home/alice"])
    return module_loader(
        "docker/af-pod-monitor/pod-metrics-exporter.py",
        extra_modules={"prometheus_client": prometheus_stub},
    )


def test_module_initializes_directories_from_non_skipped_user(
    monkeypatch, module_loader, prometheus_stub
) -> None:
    module = _load_exporter(monkeypatch, module_loader, prometheus_stub)

    assert module.username == "alice"
    assert module.directories == {
        "home": "/home/alice",
        "work": "/work/users/alice/",
    }


def test_update_metrics_work_branch_sets_usage_and_access_time(
    monkeypatch, module_loader, prometheus_stub, recording_gauge_cls
) -> None:
    module = _load_exporter(monkeypatch, module_loader, prometheus_stub)
    module.metrics = {
        "work_dir_used": recording_gauge_cls(),
        "work_dir_size": recording_gauge_cls(),
        "work_dir_util": recording_gauge_cls(),
        "work_dir_last_accessed": recording_gauge_cls(),
    }
    module.dl = "work"
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda *_args, **_kwargs: b"2048 /work/users/alice/\n",
    )
    monkeypatch.setattr(
        module.os,
        "stat",
        lambda _directory: SimpleNamespace(st_atime=1700000000.0),
    )

    module.update_metrics("work")

    assert module.metrics["work_dir_used"].values == [2048]
    assert module.metrics["work_dir_size"].values == [104857600]
    assert module.metrics["work_dir_util"].values == [2048 / 104857600]
    assert module.metrics["work_dir_last_accessed"].values == [1700000000.0]


def test_update_metrics_home_branch_parses_df_and_ignores_stat_errors(
    monkeypatch, module_loader, prometheus_stub, recording_gauge_cls
) -> None:
    module = _load_exporter(monkeypatch, module_loader, prometheus_stub)
    module.metrics = {
        "home_dir_used": recording_gauge_cls(),
        "home_dir_size": recording_gauge_cls(),
        "home_dir_util": recording_gauge_cls(),
        "home_dir_last_accessed": recording_gauge_cls(),
    }
    module.dl = "home"

    df_output = (
        "Filesystem 1K-blocks Used Available Use% Mounted on\n"
        "/dev/sda1 1000 250 750 25% /home\n"
    ).encode("utf-8")
    monkeypatch.setattr(
        module.subprocess, "check_output", lambda *_args, **_kwargs: df_output
    )

    def _raise_stat(_directory):
        raise OSError("stat unavailable")

    monkeypatch.setattr(module.os, "stat", _raise_stat)

    module.update_metrics("home")

    assert module.metrics["home_dir_used"].values == [250]
    assert module.metrics["home_dir_size"].values == [1000]
    assert module.metrics["home_dir_util"].values == [0.25]
    assert module.metrics["home_dir_last_accessed"].values == []
