from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock
from uuid import uuid4

from common import FIXTURES_ROOT, load_json_fixture, load_module_with_fake_prometheus

METRIC_FILE = "/work/projects/purdue-af/agc/metrics/event_rate.txt"
MODULE_PATH = "apps/monitoring/af-monitoring/metrics_server.py"


class MonitoringMetricUpdateFlowIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        module_name = f"metrics_server_integration_{uuid4().hex}"
        self.module = load_module_with_fake_prometheus(MODULE_PATH, module_name)

    def _patched_open_for_fixture(self, fixture_path: Path):
        real_open = open

        def _patched_open(path, *args, **kwargs):
            if str(path) == METRIC_FILE:
                return real_open(fixture_path, *args, **kwargs)
            return real_open(path, *args, **kwargs)

        return _patched_open

    def test_fixture_backed_metric_updates(self) -> None:
        cases = load_json_fixture("monitoring/event_rate_cases.json")

        for case in cases:
            fixture_path = FIXTURES_ROOT / "monitoring" / case["fixture_file"]
            with self.subTest(case=case["name"]), mock.patch(
                "builtins.open",
                side_effect=self._patched_open_for_fixture(fixture_path),
            ):
                self.module.update_metrics()
                self.assertEqual(
                    self.module.event_rate_per_worker.history[-1],
                    case["expected_gauge_value"],
                )

    def test_missing_metric_file_falls_back_to_zero(self) -> None:
        real_open = open

        def _patched_open(path, *args, **kwargs):
            if str(path) == METRIC_FILE:
                raise FileNotFoundError("event rate fixture not found")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=_patched_open):
            self.module.update_metrics()

        self.assertEqual(self.module.event_rate_per_worker.history[-1], 0)


if __name__ == "__main__":
    unittest.main()
