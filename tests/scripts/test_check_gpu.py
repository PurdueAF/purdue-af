"""Tests for pixi/global/check-gpu.py — the in-session CUDA sanity check.
Covers the status classification of each framework subprocess, the driver
gate and the exit-code decision; the GPU snippets themselves only compile."""

import subprocess
import sys

import pytest
from common import REPO, load_script


@pytest.fixture()
def check_gpu():
    return load_script(REPO / "pixi" / "global" / "check-gpu.py", "check_gpu")


def test_every_snippet_compiles(check_gpu):
    for name, code in check_gpu.CHECKS.items():
        compile(code, name, "exec")
    assert f"SystemExit({check_gpu.SKIP_EXIT})" in check_gpu.CHECKS["root"]


class TestRunCheck:
    def run(self, check_gpu, code, timeout=60):
        return check_gpu.run_check("x", code, timeout)

    def test_pass_reports_last_stdout_line(self, check_gpu):
        status, detail, _ = self.run(check_gpu, "print('warmup'); print('torch ok')")
        assert (status, detail) == ("PASS", "torch ok")

    def test_pass_without_output(self, check_gpu):
        assert self.run(check_gpu, "pass")[:2] == ("PASS", "ok")

    def test_skip_takes_reason_from_stdout_not_stderr(self, check_gpu):
        code = (
            "import sys; print('no CUDA backend');"
            " print('cling noise', file=sys.stderr); raise SystemExit(42)"
        )
        assert self.run(check_gpu, code)[:2] == ("SKIP", "no CUDA backend")

    def test_skip_without_output(self, check_gpu):
        assert self.run(check_gpu, "raise SystemExit(42)")[:2] == ("SKIP", "skipped")

    def test_fail_reports_last_error_line(self, check_gpu):
        code = "assert False, 'no GPU visible to TensorFlow'"
        status, detail, _ = self.run(check_gpu, code)
        assert status == "FAIL"
        assert detail == "AssertionError: no GPU visible to TensorFlow"

    def test_silent_fail(self, check_gpu):
        assert self.run(check_gpu, "raise SystemExit(3)")[:2] == ("FAIL", "")

    def test_glibcxx_hint(self, check_gpu):
        code = "raise ImportError('version `GLIBCXX_3.4.29` not found')"
        status, detail, _ = self.run(check_gpu, code)
        assert status == "FAIL"
        assert "GLIBCXX_3.4.29" in detail
        assert "LD_LIBRARY_PATH=$CONDA_PREFIX/lib" in detail

    def test_timeout(self, check_gpu):
        status, detail, elapsed = self.run(
            check_gpu, "import time; time.sleep(30)", timeout=0.5
        )
        assert (status, elapsed) == ("TIMEOUT", 0.5)
        assert "no result within" in detail


def _smi(check_gpu, monkeypatch, *results):
    calls = []
    replies = iter(results)

    def fake_run(cmd, **kwargs):
        calls.append(cmd[1:])
        rc, out, err = next(replies)
        return subprocess.CompletedProcess(cmd, rc, out, err)

    monkeypatch.setattr(check_gpu.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(check_gpu.subprocess, "run", fake_run)
    return calls


class TestDriverReport:
    def test_no_nvidia_smi(self, check_gpu, monkeypatch, capsys):
        monkeypatch.setattr(check_gpu.shutil, "which", lambda _: None)
        assert check_gpu.driver_report() is False
        assert "not a GPU session" in capsys.readouterr().out

    def test_query_ok(self, check_gpu, monkeypatch, capsys):
        calls = _smi(check_gpu, monkeypatch, (0, "A100, 550.54, 40960 MiB\n", ""))
        assert check_gpu.driver_report() is True
        assert len(calls) == 1
        assert "driver/GPU: A100, 550.54, 40960 MiB" in capsys.readouterr().out

    def test_mig_falls_back_to_list(self, check_gpu, monkeypatch, capsys):
        calls = _smi(
            check_gpu,
            monkeypatch,
            (6, "", "not supported"),
            (0, "GPU 0: A100 (UUID: x)\n  MIG 1g.5gb\n", ""),
        )
        assert check_gpu.driver_report() is True
        assert calls[1] == ["-L"]
        assert "MIG 1g.5gb" in capsys.readouterr().out

    def test_empty_output_still_passes(self, check_gpu, monkeypatch, capsys):
        _smi(check_gpu, monkeypatch, (0, "", ""))
        assert check_gpu.driver_report() is True
        assert "(no output)" in capsys.readouterr().out

    def test_driver_broken(self, check_gpu, monkeypatch, capsys):
        _smi(
            check_gpu,
            monkeypatch,
            (9, "", "x"),
            (9, "", "NVIDIA-SMI has failed\n"),
        )
        assert check_gpu.driver_report() is False
        assert "NVIDIA-SMI has failed" in capsys.readouterr().out


class TestMain:
    @pytest.fixture()
    def ran(self, check_gpu, monkeypatch):
        ran = []
        monkeypatch.setattr(check_gpu, "driver_report", lambda: True)
        monkeypatch.setattr(sys, "argv", ["check-gpu.py"])
        return ran

    def fake_statuses(self, check_gpu, monkeypatch, ran, statuses):
        def fake(name, code, timeout):
            ran.append((name, timeout))
            return statuses.get(name, "PASS"), "detail", 1.0

        monkeypatch.setattr(check_gpu, "run_check", fake)

    def test_all_pass_runs_every_check(self, check_gpu, monkeypatch, ran, capsys):
        self.fake_statuses(check_gpu, monkeypatch, ran, {})
        assert check_gpu.main() == 0
        assert [n for n, _ in ran] == list(check_gpu.CHECKS)
        assert "all good" in capsys.readouterr().out

    def test_skip_is_not_a_failure(self, check_gpu, monkeypatch, ran):
        self.fake_statuses(check_gpu, monkeypatch, ran, {"root": "SKIP"})
        assert check_gpu.main() == 0

    @pytest.mark.parametrize("status", ["FAIL", "TIMEOUT"])
    def test_failure_or_timeout_fails(
        self, check_gpu, monkeypatch, ran, capsys, status
    ):
        self.fake_statuses(check_gpu, monkeypatch, ran, {"torch": status})
        assert check_gpu.main() == 1
        out = capsys.readouterr().out
        assert f"{status:<7} torch" in out
        assert "FAILURES above" in out
        # one failure does not stop the remaining checks
        assert len(ran) == len(check_gpu.CHECKS)

    def test_only_and_timeout(self, check_gpu, monkeypatch, ran):
        self.fake_statuses(check_gpu, monkeypatch, ran, {})
        monkeypatch.setattr(
            sys, "argv", ["x", "--only", " numba, torch ,", "--timeout", "5"]
        )
        assert check_gpu.main() == 0
        assert ran == [("numba", 5.0), ("torch", 5.0)]

    def test_unknown_check_is_a_usage_error(self, check_gpu, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["x", "--only", "torch,jax"])
        with pytest.raises(SystemExit) as exc:
            check_gpu.main()
        assert exc.value.code == 2
        assert "unknown check(s): jax" in capsys.readouterr().err

    def test_no_driver_skips_checks(self, check_gpu, monkeypatch, ran):
        self.fake_statuses(check_gpu, monkeypatch, ran, {})
        monkeypatch.setattr(check_gpu, "driver_report", lambda: False)
        assert check_gpu.main() == 1
        assert ran == []
