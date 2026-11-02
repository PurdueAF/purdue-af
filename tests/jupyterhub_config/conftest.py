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
        # (use_ssl, "start_tls" | "bind") per connection
        "sessions": [],
        # DNs the directory does not hold: a read of them comes back empty
        "missing_dns": set(),
        # (connect_timeout, receive_timeout) per connection
        "timeouts": [],
        "unbinds": 0,
        # raised by the named Connection method
        "failures": {},
    }

    def may_fail(step):
        if step in state["failures"]:
            raise state["failures"][step]

    class FakeConnection:
        def __init__(self, server, version, authentication, receive_timeout):
            self.use_ssl, connect_timeout = server
            state["timeouts"].append((connect_timeout, receive_timeout))
            self.found = False

        def start_tls(self):
            may_fail("start_tls")
            state["sessions"].append((self.use_ssl, "start_tls"))

        def bind(self):
            may_fail("bind")
            state["sessions"].append((self.use_ssl, "bind"))

        def unbind(self):
            state["unbinds"] += 1

        def search(self, search_base, search_filter, search_scope, attributes):
            may_fail("search")
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

    def fake_server(host, use_ssl, get_info, connect_timeout):
        state["hosts"].append(host)
        return use_ssl, connect_timeout

    ldap3 = types.ModuleType("ldap3")
    ldap3.BASE = "BASE"
    ldap3.Server = fake_server
    ldap3.Connection = FakeConnection
    monkeypatch.setitem(sys.modules, "ldap3", ldap3)
    return state
