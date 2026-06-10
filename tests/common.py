"""Plumbing shared by the test suites."""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_script(path, module_name):
    """Import a script with a non-importable filename (dashes)."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ConfigSink(dict):
    """Accepts both attribute traversal (c.ServerApp.ip = 1) and dict ops
    (c.KubeSpawner.environment.setdefault(...)), like a traitlets config."""

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
