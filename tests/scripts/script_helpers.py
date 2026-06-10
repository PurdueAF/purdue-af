"""Test helpers for standalone-script tests.

jupyter_server_config.py is exec'd by `jupyter server` with `get_config()`
in scope — replicated here with the shared ConfigSink, so the file under
test is byte-identical to what ships in the image.
"""

import sys
import types

from common import REPO, ConfigSink

JUPYTER_CONFIG = REPO / "docker" / "purdue-af" / "jupyter" / "jupyter_server_config.py"


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
