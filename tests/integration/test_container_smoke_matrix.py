from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from common import REPO_ROOT, load_json_fixture

CONTAINER_SMOKE_SCRIPT = REPO_ROOT / ".github/scripts/container-smoke.sh"
MOCK_DOCKER_SCRIPT = REPO_ROOT / ".github/scripts/integration/mock-docker-cli.sh"


class ContainerSmokeBehaviorMatrixIntegrationTest(unittest.TestCase):
    def test_container_smoke_behavior_matrix(self) -> None:
        cases = load_json_fixture("container_smoke/matrix.json")

        for case in cases:
            with self.subTest(case=case["name"]):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    docker_wrapper = temp_path / "docker"
                    docker_wrapper.write_text(
                        f'#!/usr/bin/env bash\nexec "{MOCK_DOCKER_SCRIPT}" "$@"\n',
                        encoding="utf-8",
                    )
                    docker_wrapper.chmod(0o755)

                    log_file = temp_path / "docker.log"
                    env = os.environ.copy()
                    env["PATH"] = f"{temp_path}:{env.get('PATH', '')}"
                    env["MOCK_DOCKER_LOG"] = str(log_file)
                    env["MOCK_DOCKER_INSPECT_EXIT"] = str(case["mock"]["inspect_exit"])
                    env["MOCK_DOCKER_RUN_EXIT"] = str(case["mock"]["run_exit"])
                    env["MOCK_DOCKER_INSPECT_STDERR"] = case["mock"].get(
                        "inspect_stderr", ""
                    )
                    env["MOCK_DOCKER_RUN_STDERR"] = case["mock"].get("run_stderr", "")

                    result = subprocess.run(
                        [
                            "bash",
                            str(CONTAINER_SMOKE_SCRIPT),
                            case["image"],
                            case["profile"],
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        env=env,
                    )

                    expected = case["expected"]
                    self.assertEqual(result.returncode, expected["exit_code"])
                    for expected_text in expected["stdout_contains"]:
                        self.assertIn(expected_text, result.stdout)
                    for expected_text in expected["stderr_contains"]:
                        self.assertIn(expected_text, result.stderr)

                    logged_lines = []
                    if log_file.exists():
                        logged_lines = log_file.read_text(encoding="utf-8").splitlines()
                    self.assertEqual(logged_lines, expected["log_lines"])


if __name__ == "__main__":
    unittest.main()
