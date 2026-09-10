"""Fixtures for the JupyterHub config suite (helpers in hub_helpers.py)."""

import sys
import types

import pytest


@pytest.fixture
def fake_ldap(monkeypatch):
    """Install a fake `ldap3` module; returns a dict to configure responses."""
    state = {
        "uid": 12345,
        "gid": 67890,
        "searches": [],
        "bases": [],
        "scopes": [],
        "hosts": [],
        # DNs the directory does not hold at their own entry, so a base-scope
        # read of them comes back empty
        "missing_dns": set(),
    }

    class FakeConnection:
        def __init__(self, server, version, authentication):
            self.found = False

        def start_tls(self):
            pass

        def search(self, search_base, search_filter, search_scope, attributes):
            state["searches"].append(search_filter)
            state["bases"].append(search_base)
            state["scopes"].append(search_scope)
            self.found = search_base not in state["missing_dns"]

        def response_to_json(self):
            import json

            entries = (
                [
                    {
                        "attributes": {
                            "uidNumber": state["uid"],
                            "gidNumber": state["gid"],
                        }
                    }
                ]
                if self.found
                else []
            )
            return json.dumps({"entries": entries})

    def fake_server(host, use_ssl, get_info):
        state["hosts"].append(host)
        return None

    ldap3 = types.ModuleType("ldap3")
    ldap3.BASE = "BASE"
    ldap3.SUBTREE = "SUBTREE"
    ldap3.Server = fake_server
    ldap3.Connection = FakeConnection
    monkeypatch.setitem(sys.modules, "ldap3", ldap3)
    return state
