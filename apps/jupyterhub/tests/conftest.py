"""Harness for testing JupyterHub extraFiles config snippets.

The snippets are not importable modules: the hub `exec`s them with a config
object `c` in scope. We replicate that here with a permissive stub, so the
files under test are byte-identical to what runs in production.
"""

import sys
import types
from pathlib import Path

import pytest

EXTRA_FILES = Path(__file__).resolve().parent.parent / "jupyterhub" / "extraFiles"


class ConfigSink(dict):
    """Accepts both attribute traversal (c.KubeSpawner.x = 1) and dict ops
    (c.KubeSpawner.environment.setdefault(...)), like traitlets config."""

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


def load_snippet(filename, monkeypatch, namespace="cms", extra_globals=None):
    """Exec an extraFiles snippet the way JupyterHub does; return its globals."""
    monkeypatch.setenv("POD_NAMESPACE", namespace)
    ns = {"c": ConfigSink()}
    if extra_globals:
        ns.update(extra_globals)
    code = (EXTRA_FILES / filename).read_text()
    exec(compile(code, str(EXTRA_FILES / filename), "exec"), ns)
    return ns


@pytest.fixture
def fake_ldap(monkeypatch):
    """Install a fake `ldap3` module; returns a dict to configure responses."""
    state = {"uid": 12345, "gid": 67890, "searches": []}

    class FakeConnection:
        def __init__(self, server, version, authentication):
            pass

        def start_tls(self):
            pass

        def search(self, search_base, search_filter, search_scope, attributes):
            state["searches"].append(search_filter)

        def response_to_json(self):
            import json

            return json.dumps(
                {
                    "entries": [
                        {
                            "attributes": {
                                "uidNumber": state["uid"],
                                "gidNumber": state["gid"],
                            }
                        }
                    ]
                }
            )

    ldap3 = types.ModuleType("ldap3")
    ldap3.SUBTREE = "SUBTREE"
    ldap3.Server = lambda host, use_ssl, get_info: None
    ldap3.Connection = FakeConnection
    monkeypatch.setitem(sys.modules, "ldap3", ldap3)
    return state


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
