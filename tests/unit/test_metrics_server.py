from __future__ import annotations

from unittest.mock import mock_open


def test_update_metrics_sets_event_rate_from_file(
    monkeypatch, module_loader, prometheus_stub, recording_gauge_cls
) -> None:
    module = module_loader(
        "apps/monitoring/af-monitoring/metrics_server.py",
        extra_modules={"prometheus_client": prometheus_stub},
    )
    module.event_rate_per_worker = recording_gauge_cls()
    monkeypatch.setattr("builtins.open", mock_open(read_data="42.5\n"))

    module.update_metrics()

    assert module.event_rate_per_worker.values == [42.5]


def test_update_metrics_sets_zero_when_read_fails(
    monkeypatch, module_loader, prometheus_stub, recording_gauge_cls
) -> None:
    module = module_loader(
        "apps/monitoring/af-monitoring/metrics_server.py",
        extra_modules={"prometheus_client": prometheus_stub},
    )
    module.event_rate_per_worker = recording_gauge_cls()

    def _raise(*_args, **_kwargs):
        raise OSError("not found")

    monkeypatch.setattr("builtins.open", _raise)

    module.update_metrics()

    assert module.event_rate_per_worker.values == [0]
