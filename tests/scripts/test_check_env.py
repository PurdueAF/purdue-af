"""Tests for pixi/check-env.py — the post-install import smoke run by
ci-pixi-base.yml, ci-pixi-global.yml and the image build. Import-name
derivation, per-package subprocess outcome and the exit-code decision."""

import json
import runpy
import subprocess
import sys

import pytest
from common import REPO, load_script

SCRIPT = REPO / "pixi" / "check-env.py"


@pytest.fixture()
def check_env():
    return load_script(SCRIPT, "check_env")


MANIFEST = """\
[workspace]
name = "x"
platforms = [
    "linux-64",
]

[dependencies]
python = "3.12.*"  # comment
root = "*"
gsl = "*"

[pypi-dependencies]
tf-keras = ">=2"
"""


class TestParseManifest:
    def test_tomllib(self, check_env, tmp_path):
        path = tmp_path / "pixi.toml"
        path.write_text(MANIFEST)
        assert check_env.parse_manifest(path) == (
            ["python", "root", "gsl"],
            ["tf-keras"],
        )

    def test_line_parser_fallback(self, check_env, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "tomllib", None)
        path = tmp_path / "pixi.toml"
        path.write_text(MANIFEST)
        assert check_env.parse_manifest(path) == (
            ["python", "root", "gsl"],
            ["tf-keras"],
        )


def _meta(env_dir, name, files=(), depends=()):
    meta = env_dir / "conda-meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / f"{name}-1.0-0.json").write_text(
        json.dumps({"name": name, "files": list(files), "depends": list(depends)})
    )


SP = "lib/python3.12/site-packages"


class TestCondaToplevels:
    def test_derives_import_names(self, check_env, tmp_path):
        _meta(
            tmp_path,
            "root",
            [
                f"{SP}/ROOT/__init__.py",
                f"{SP}/cppyy.py",
                f"{SP}/libcppyy.cpython-312-x86_64-linux-gnu.so",
                f"{SP}/_private/__init__.py",
                f"{SP}/tests/__init__.py",
                f"{SP}/ROOT/sub/__init__.py",
                "bin/root",
            ],
        )
        _meta(tmp_path, "gsl", ["lib/libgsl.so"])
        assert check_env.conda_toplevels(tmp_path) == {
            "root": ["ROOT", "cppyy", "libcppyy"],
            "gsl": [],
        }

    def test_metapackage_follows_one_level_of_depends(self, check_env, tmp_path):
        _meta(tmp_path, "matplotlib", [], ["matplotlib-base >=3.9", "tornado"])
        _meta(tmp_path, "matplotlib-base", [f"{SP}/matplotlib/__init__.py"])
        _meta(tmp_path, "tornado", [f"{SP}/tornado/__init__.py"])
        assert check_env.conda_toplevels(tmp_path)["matplotlib"] == [
            "matplotlib",
            "tornado",
        ]

    def test_skips_unparseable_records(self, check_env, tmp_path):
        _meta(tmp_path, "ok", [f"{SP}/ok.py"])
        (tmp_path / "conda-meta" / "broken.json").write_text("{not json")
        assert check_env.conda_toplevels(tmp_path) == {"ok": ["ok"]}


class _Dist:
    def __init__(self, name, top_level=None, files=()):
        self.metadata = {"Name": name}
        self._top_level = top_level
        self.files = list(files)

    def read_text(self, filename):
        return self._top_level if filename == "top_level.txt" else None


class TestPypiToplevels:
    @pytest.fixture()
    def dists(self, monkeypatch):
        found = []
        monkeypatch.setattr(
            "importlib.metadata.distributions", lambda: iter(found), raising=True
        )
        return found

    def test_top_level_txt_with_normalised_name(self, check_env, dists):
        dists += [
            _Dist("other", "other\n"),
            _Dist("TF_Keras", "tf_keras\n_private\ntests\n\n"),
        ]
        assert check_env.pypi_toplevels("tf.keras") == ["tf_keras"]

    def test_record_files_fallback(self, check_env, dists):
        dists.append(
            _Dist(
                "hepdata-lib",
                files=[
                    "hepdata_lib/__init__.py",
                    "hepdata_lib/helpers.py",
                    "single.py",
                    "compiled.cpython-312-x86_64-linux-gnu.so",
                    "_hidden.py",
                    "hepdata_lib-1.0.dist-info/METADATA",
                ],
            )
        )
        assert check_env.pypi_toplevels("hepdata_lib") == [
            "compiled",
            "hepdata_lib",
            "single",
        ]

    def test_not_installed(self, check_env, dists):
        assert check_env.pypi_toplevels("absent") is None


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TestImportCheck:
    def test_real_interpreter_pass_and_fail(self, check_env):
        ok, _, detail = check_env.import_check(sys.executable, ["json", "csv"], 60)
        assert (ok, detail) == (True, "")
        ok, _, detail = check_env.import_check(
            sys.executable, ["json", "no_such_module_xyz"], 60
        )
        assert not ok and "No module named 'no_such_module_xyz'" in detail

    def test_isolated_env_and_one_statement_per_module(self, check_env, monkeypatch):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(cmd=cmd, **kwargs)
            return _completed(0)

        monkeypatch.setattr(check_env.subprocess, "run", fake_run)
        check_env.import_check("/env/python", ["a", "b"], 5)
        assert seen["cmd"] == ["/env/python", "-c", "import a; import b"]
        env = seen["env"]
        assert env["HOME"] == env["TMPDIR"]
        assert "check-env-" in env["HOME"]
        assert env["MPLBACKEND"] == "Agg"
        assert seen["timeout"] == 5

    def test_keeps_a_30_line_tail_and_flags_glibcxx(self, check_env, monkeypatch):
        out = "".join(f"line{i}\n" for i in range(40))
        err = "ImportError: /usr/lib64/libstdc++.so.6: version `GLIBCXX_3.4.29'"
        monkeypatch.setattr(
            check_env.subprocess, "run", lambda *a, **k: _completed(1, out, err)
        )
        ok, _, detail = check_env.import_check("py", ["tensorflow"], 5)
        assert not ok
        assert "line10" not in detail and "line11" in detail
        assert detail.endswith("see check-gpu.py]")

    def test_silent_failure_reports_exit_code(self, check_env, monkeypatch):
        monkeypatch.setattr(
            check_env.subprocess, "run", lambda *a, **k: _completed(-11)
        )
        assert check_env.import_check("py", ["ROOT"], 5)[2] == "exit -11"

    def test_timeout(self, check_env, monkeypatch):
        def hang(cmd, timeout, **kwargs):
            raise subprocess.TimeoutExpired(cmd, timeout)

        monkeypatch.setattr(check_env.subprocess, "run", hang)
        assert check_env.import_check("py", ["ROOT"], 7) == (
            False,
            7,
            "import hung for 7s",
        )


@pytest.fixture()
def env_dir(tmp_path):
    (tmp_path / "pixi.toml").write_text(MANIFEST)
    env = tmp_path / ".pixi" / "envs" / "default"
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").touch()
    _meta(env, "python", ["lib/python3.12/os.py"])
    _meta(env, "root", [f"{SP}/ROOT/__init__.py"])
    _meta(env, "gsl", ["lib/libgsl.so"])
    return tmp_path


def _run_main(check_env, monkeypatch, env_dir, pypi, failing=()):
    calls = []

    def fake_resolver(cmd, **kwargs):
        assert cmd[0] == str(env_dir / ".pixi/envs/default/bin/python")
        assert cmd[2:] == ["--_resolve-pypi", "tf-keras"]
        return _completed(0, json.dumps(pypi))

    def fake_import(python, modules, timeout):
        calls.append(modules)
        return (modules[0] not in failing, 0.1, "boom")

    monkeypatch.setattr(check_env.subprocess, "run", fake_resolver)
    monkeypatch.setattr(check_env, "import_check", fake_import)
    monkeypatch.setattr(
        sys, "argv", ["check-env.py", "--manifest", str(env_dir / "pixi.toml")]
    )
    return check_env.main(), calls


class TestMain:
    def test_all_ok(self, check_env, monkeypatch, env_dir, capsys):
        rc, calls = _run_main(
            check_env, monkeypatch, env_dir, {"tf-keras": ["tf_keras"]}
        )
        out = capsys.readouterr().out
        assert rc == 0
        # python's stdlib is not under site-packages -> no-python, like gsl
        assert sorted(calls) == [["ROOT"], ["tf_keras"]]
        assert "4 declared | 2 importable | 2 no-python | 0 NOT INSTALLED" in out
        assert "no python modules (skipped): gsl, python" in out
        assert out.rstrip().endswith("all imports OK")

    def test_import_failure_fails(self, check_env, monkeypatch, env_dir, capsys):
        rc, _ = _run_main(
            check_env,
            monkeypatch,
            env_dir,
            {"tf-keras": ["tf_keras"]},
            failing={"ROOT"},
        )
        out = capsys.readouterr().out
        assert rc == 1
        assert "FAIL  root" in out and "boom" in out
        assert "FAILURES: root" in out

    def test_uninstalled_pypi_dep_fails(self, check_env, monkeypatch, env_dir, capsys):
        rc, _ = _run_main(check_env, monkeypatch, env_dir, {"tf-keras": None})
        out = capsys.readouterr().out
        assert rc == 1
        assert "not installed in the env" in out
        assert "FAILURES: tf-keras" in out

    def test_conda_dep_absent_from_env_fails(self, check_env, monkeypatch, env_dir):
        for meta in (env_dir / ".pixi/envs/default/conda-meta").glob("root-*"):
            meta.unlink()
        rc, _ = _run_main(check_env, monkeypatch, env_dir, {"tf-keras": ["tf_keras"]})
        assert rc == 1

    def test_missing_manifest(self, check_env, monkeypatch, tmp_path):
        monkeypatch.setattr(
            sys, "argv", ["x", "--manifest", str(tmp_path / "pixi.toml")]
        )
        with pytest.raises(SystemExit, match="no .*pixi.toml"):
            check_env.main()

    def test_missing_interpreter(self, check_env, monkeypatch, env_dir):
        monkeypatch.setattr(
            sys,
            "argv",
            ["x", "--manifest", str(env_dir / "pixi.toml"), "--env", "gpu"],
        )
        with pytest.raises(SystemExit, match="has `pixi install` run"):
            check_env.main()


def test_resolve_pypi_mode(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--_resolve-pypi", "pytest", "nope"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0
    resolved = json.loads(capsys.readouterr().out)
    assert "pytest" in resolved["pytest"]
    assert resolved["nope"] is None
