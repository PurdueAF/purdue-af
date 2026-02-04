from __future__ import annotations

import subprocess
import unittest
from unittest import mock
from uuid import uuid4

from common import load_json_fixture, load_module_with_fake_prometheus

MODULE_PATH = "apps/monitoring/af-monitoring/node_healthcheck.py"


class FakeMd5Process:
    def __init__(self, case: dict):
        self.mode = case["mode"]
        self.stdout = case["md5_stdout"]
        self.stderr = case["md5_stderr"]
        self.returncode = case["returncode"]
        self.killed = False
        self.communicate_calls = 0
        self.timeout_history: list[float | int | None] = []

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        self.timeout_history.append(timeout)
        if self.mode == "timeout" and self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(cmd="/usr/bin/md5sum", timeout=timeout)
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


class NodeHealthChecksumTimeoutIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        module_name = f"node_healthcheck_integration_{uuid4().hex}"
        self.module = load_module_with_fake_prometheus(MODULE_PATH, module_name)

    def test_checksum_and_timeout_matrix(self) -> None:
        cases = load_json_fixture("node_health/checksum_cases.json")

        for case in cases:
            process = FakeMd5Process(case)
            time_values = [case["start_time"]]
            if case["mode"] != "timeout":
                time_values.append(case["end_time"])

            with self.subTest(case=case["name"]), mock.patch.object(
                self.module.subprocess,
                "Popen",
                return_value=process,
            ) as popen_mock, mock.patch.object(
                self.module.time,
                "time",
                side_effect=time_values,
            ):
                result, ping_ms = self.module.check_if_directory_exists(
                    (case["filename"], case["expected_checksum"])
                )

            self.assertEqual(result, case["expected_result"])
            self.assertEqual(process.killed, case["expect_killed"])
            self.assertEqual(
                popen_mock.call_args[0][0],
                ["/usr/bin/md5sum", case["filename"]],
            )
            if case["mode"] == "timeout":
                self.assertEqual(process.timeout_history, [3, None])
            else:
                self.assertEqual(process.timeout_history, [3])

            expected_ping_ms = case["expected_ping_ms"]
            if isinstance(expected_ping_ms, float):
                self.assertAlmostEqual(ping_ms, expected_ping_ms, delta=0.001)
            else:
                self.assertEqual(ping_ms, expected_ping_ms)

            if case["mode"] == "timeout":
                self.assertEqual(process.communicate_calls, 2)
            else:
                self.assertEqual(process.communicate_calls, 1)


if __name__ == "__main__":
    unittest.main()
