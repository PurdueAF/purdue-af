"""Test helpers for standalone-script tests.

jupyter_server_config.py is exec'd by `jupyter server` with `get_config()`
in scope — replicated here with the shared ConfigSink, so the file under
test is byte-identical to what ships in the image.
"""

from common import REPO, ConfigSink

JUPYTER_CONFIG = REPO / "docker" / "purdue-af" / "jupyter" / "jupyter_server_config.py"


def load_jupyter_config(monkeypatch, env=None):
    """Exec jupyter_server_config.py the way `jupyter server` does; return
    (globals, config sink).

    Every session variable the config reads is cleared unless a test sets it,
    so a developer's own shell cannot decide what the config under test
    produces."""
    for var in ("NB_UMASK", "JUPYTERHUB_API_TOKEN", "NAMESPACE"):
        monkeypatch.delenv(var, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    sink = ConfigSink()
    ns = {"get_config": lambda: sink, "__name__": "jupyter_server_config"}
    code = JUPYTER_CONFIG.read_text()
    exec(compile(code, str(JUPYTER_CONFIG), "exec"), ns)
    return ns, sink
