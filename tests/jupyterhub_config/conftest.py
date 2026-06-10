"""Fixtures for the JupyterHub config suite (helpers in hub_helpers.py)."""

import sys
import types

import pytest


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
