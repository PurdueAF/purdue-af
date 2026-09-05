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
        "hosts": [],
        # authentication mode of every Connection created
        "binds": [],
        "bind_ok": True,
        # LDAP result of the last operation; 0 = success, 32 = noSuchObject
        "result": {"result": 0, "description": "success", "message": ""},
        # None -> one entry built from uid/gid; [] -> user not found
        "entries": None,
    }

    class FakeConnection:
        def __init__(self, server, version, authentication, receive_timeout=None):
            state["binds"].append(authentication)
            self.result = {"result": 0, "description": "success", "message": ""}

        def start_tls(self):
            pass

        def bind(self):
            if not state["bind_ok"]:
                self.result = {
                    "result": 49,
                    "description": "invalidCredentials",
                    "message": "",
                }
            return state["bind_ok"]

        def search(self, search_base, search_filter, search_scope, attributes):
            state["searches"].append(search_filter)
            state["bases"].append(search_base)
            self.result = dict(state["result"])

        def response_to_json(self):
            import json

            entries = state["entries"]
            if entries is None:
                entries = [
                    {
                        "attributes": {
                            "uidNumber": state["uid"],
                            "gidNumber": state["gid"],
                        }
                    }
                ]
            return json.dumps({"entries": entries})

    def fake_server(host, use_ssl, get_info):
        state["hosts"].append(host)
        return None

    ldap3 = types.ModuleType("ldap3")
    ldap3.SUBTREE = "SUBTREE"
    ldap3.Server = fake_server
    ldap3.Connection = FakeConnection
    monkeypatch.setitem(sys.modules, "ldap3", ldap3)
    return state
