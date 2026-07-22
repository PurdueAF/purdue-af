"""refresh_user contract: login-time auth_state must stay authoritative.

oauthenticator >= 17.2 (hub images of chart >= 4.1.0) implements
refresh_user by rebuilding auth_state from the raw CILogon data, dropping
the name/domain keys that authenticate()/post_auth_hook inject at login;
set-user-info.py's auth_state_hook then KeyErrors and every spawn 500s.
This broke the 2025-09 (4.2.0) and 2026-06 (4.3.5) chart upgrades.
custom-spawner.py pins refresh_user to "no change" — this test pins the pin.
"""

import asyncio

from hub_helpers import load_snippet


def test_refresh_user_reports_no_change(monkeypatch):
    ns = load_snippet("custom-spawner.py", monkeypatch)
    cls = ns["PurdueCILogonOAuthenticator"]
    # call unbound to sidestep CILogonOAuthenticator's required config;
    # the override must not consult self at all
    result = asyncio.run(cls.refresh_user(None, user=object()))
    assert result is True
