"""refresh_user contract: the identity from login stands.

custom-spawner.py pins refresh_user to "no change", so a session is never
re-validated against CILogon between logins.
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
