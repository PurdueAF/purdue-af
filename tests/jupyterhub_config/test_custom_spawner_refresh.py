"""refresh_user contract: login-time auth_state must stay authoritative.

oauthenticator's refresh_user rebuilds auth_state from the raw CILogon data,
dropping the name/domain keys injected at login that set-user-info.py's
auth_state_hook needs. custom-spawner.py pins refresh_user to "no change".
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
