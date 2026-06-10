"""Tests for apps/monitoring/af-monitoring/metrics_server.py."""

import metrics_server
from prometheus_client import REGISTRY


def rate():
    return REGISTRY.get_sample_value("agc_event_rate_per_worker")


def test_reads_event_rate(tmp_path, monkeypatch):
    f = tmp_path / "event_rate.txt"
    f.write_text(" 12.5 \n")
    monkeypatch.setattr(metrics_server, "EVENT_RATE_FILE", str(f))

    metrics_server.update_metrics()
    assert rate() == 12.5


def test_missing_file_resets_to_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics_server, "EVENT_RATE_FILE", str(tmp_path / "nope.txt"))

    metrics_server.update_metrics()
    assert rate() == 0


def test_garbage_content_resets_to_zero(tmp_path, monkeypatch):
    f = tmp_path / "event_rate.txt"
    f.write_text("not-a-number")
    monkeypatch.setattr(metrics_server, "EVENT_RATE_FILE", str(f))

    metrics_server.update_metrics()
    assert rate() == 0
