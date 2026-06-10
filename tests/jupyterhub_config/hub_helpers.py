"""Test helpers for the JupyterHub extraFiles config snippets.

The snippets are not importable modules: the hub `exec`s them with a config
object `c` in scope. We replicate that here (ConfigSink), so the files under
test are byte-identical to what runs in production.
"""

import types

from common import REPO, ConfigSink

EXTRA_FILES = REPO / "apps" / "jupyterhub" / "jupyterhub" / "extraFiles"


def load_snippet(filename, monkeypatch, namespace="cms", extra_globals=None):
    """Exec an extraFiles snippet the way JupyterHub does; return its globals."""
    monkeypatch.setenv("POD_NAMESPACE", namespace)
    ns = {"c": ConfigSink()}
    if extra_globals:
        ns.update(extra_globals)
    code = (EXTRA_FILES / filename).read_text()
    exec(compile(code, str(EXTRA_FILES / filename), "exec"), ns)
    return ns


class FakeSpawner:
    """Minimal KubeSpawner stand-in for hook tests."""

    def __init__(self, user_id=1):
        self.environment = {}
        self.userdata = None
        self._auth_state = None

        async def get_auth_state():  # hub API: spawner.user.get_auth_state()
            return self._auth_state

        self.user = types.SimpleNamespace(id=user_id, get_auth_state=get_auth_state)

    def set_auth_state(self, auth_state):
        self._auth_state = auth_state
