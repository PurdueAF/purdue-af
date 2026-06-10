"""Harness for standalone-script tests.

Covers two kinds of non-importable Python:
- flat scripts with dashed filenames (plot-af-users.py), loaded via importlib;
- jupyter_server_config.py, which `jupyter server` execs with `get_config()`
  in scope — replicated here with a permissive stub (same pattern as
  apps/jupyterhub/tests), so the file under test is byte-identical to what
  ships in the image.
"""

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
JUPYTER_CONFIG = REPO / "docker" / "purdue-af" / "jupyter" / "jupyter_server_config.py"

# Headless backend for matplotlib (the cron host has no display either).
os.environ.setdefault("MPLBACKEND", "Agg")


def load_script(path, module_name):
    """Import a script with a non-importable filename (dashes)."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def plot_af_users():
    return load_script(
        REPO / "apps" / "af-utils" / "af-users-graph" / "plot-af-users.py",
        "plot_af_users",
    )


class ConfigSink(dict):
    """Accepts both attribute traversal (c.ServerApp.ip = ...) and dict ops,
    like a traitlets config object."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        child = self.get(name)
        if child is None:
            child = ConfigSink()
            self[name] = child
        return child

    def __setattr__(self, name, value):
        self[name] = value


def load_jupyter_config(monkeypatch, data_dir, env=None):
    """Exec jupyter_server_config.py the way `jupyter server` does; return
    (globals, config sink). jupyter_core is faked so jupyter_data_dir() points
    at data_dir."""
    for var in ("GEN_CERT", "NB_UMASK"):
        monkeypatch.delenv(var, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    paths_mod = types.ModuleType("jupyter_core.paths")
    paths_mod.jupyter_data_dir = lambda: str(data_dir)
    core_mod = types.ModuleType("jupyter_core")
    core_mod.paths = paths_mod
    monkeypatch.setitem(sys.modules, "jupyter_core", core_mod)
    monkeypatch.setitem(sys.modules, "jupyter_core.paths", paths_mod)

    sink = ConfigSink()
    ns = {"get_config": lambda: sink, "__name__": "jupyter_server_config"}
    code = JUPYTER_CONFIG.read_text()
    exec(compile(code, str(JUPYTER_CONFIG), "exec"), ns)
    return ns, sink
