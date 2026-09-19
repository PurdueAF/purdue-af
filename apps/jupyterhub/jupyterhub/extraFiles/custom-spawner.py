import os
from typing import Any

from oauthenticator.cilogon import CILogonOAuthenticator
from tornado import web

# JupyterHub injects `c` at exec time; the annotation is for type checkers only.
c: Any


class PurdueCILogonOAuthenticator(CILogonOAuthenticator):
    async def authenticate(self, handler: Any, data: Any = None) -> Any:
        ret = await super().authenticate(handler, data)
        username, domain = ret["auth_state"]["cilogon_user"]["eppn"].split("@")
        fixedUsername = None

        if domain == "purdue.edu":
            fixedUsername = username
            with open("/etc/secrets/af-auth-purdue/userlist") as file:
                if f"{username}\n" not in file.readlines():
                    raise web.HTTPError(
                        500,
                        f"Access denied! User {username} is not in the list of authorized users.",
                    )

        elif domain == "cern.ch":
            fixedUsername = username + "-cern"
            with open("/etc/secrets/af-auth-cern/userlist") as file:
                if f"{username}\n" not in file.readlines():
                    raise web.HTTPError(
                        500,
                        "Access denied! Only CMS members are allowed to log in with CERN credentials.",
                    )

        elif domain == "fnal.gov":
            fixedUsername = username + "-fnal"
        else:
            raise web.HTTPError(500, "Failed to get username from CILogon")

        ret["name"] = fixedUsername
        ret["domain"] = domain
        os.environ["USERNAME"] = fixedUsername
        return ret

    async def refresh_user(self, user: Any, handler: Any = None, **kwargs: Any) -> Any:
        # oauthenticator >= 17.2 would drop the auth_state keys set-user-info.py reads.
        return True


def passthrough_post_auth_hook(
    authenticator: Any, handler: Any, authentication: Any
) -> Any:
    if authentication["auth_state"] is None:
        authentication["auth_state"] = {}
    authentication["auth_state"]["name"] = authentication["name"]
    authentication["auth_state"]["domain"] = authentication["domain"]
    return authentication


c.JupyterHub.authenticator_class = PurdueCILogonOAuthenticator
c.PurdueCILogonOAuthenticator.post_auth_hook = passthrough_post_auth_hook

if os.environ["POD_NAMESPACE"] == "cms":
    c.KubeSpawner.environment.setdefault(
        "DASK_GATEWAY__ADDRESS", "http://dask-gateway-k8s.geddes.rcac.purdue.edu"
    )
    c.KubeSpawner.environment.setdefault(
        "DASK_GATEWAY__PROXY_ADDRESS",
        "traefik-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8786",
    )
