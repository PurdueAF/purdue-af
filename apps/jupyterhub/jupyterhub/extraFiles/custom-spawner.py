import os
from typing import Any

from jupyterhub.utils import maybe_future
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


async def drop_stale_user_options(spawner: Any, user_options: dict[str, Any]) -> None:
    """Map a profile slug or choice missing from profile_list onto its default.

    A spawn request without a body reuses the user's saved user_options, which
    can name a profile or choice that no longer exists.
    """
    profile_list = spawner.profile_list
    if callable(profile_list):
        profile_list = await maybe_future(profile_list(spawner))
    profiles = spawner._get_initialized_profile_list(profile_list)
    if not profiles:
        return

    options = dict(user_options)
    slug = options.get("profile")
    default = next(p for p in profiles if p.get("default"))
    profile = (
        next((p for p in profiles if p["slug"] == slug), None) if slug else default
    )
    if profile is None:
        spawner.log.warning(
            "Spawning %s on profile %s: profile %s no longer exists",
            spawner._log_name,
            default["slug"],
            slug,
        )
        profile = default
        options["profile"] = default["slug"]

    for name, option in profile.get("profile_options", {}).items():
        if not options.get(name):
            continue
        choices = {str(key): key for key in option.get("choices", {})}
        if str(options[name]) in choices:
            options[name] = choices[str(options[name])]
        else:
            spawner.log.warning(
                "Spawning %s with the default %s: choice %s no longer exists",
                spawner._log_name,
                name,
                options.pop(name),
            )

    spawner.user_options = options


c.JupyterHub.authenticator_class = PurdueCILogonOAuthenticator
c.PurdueCILogonOAuthenticator.post_auth_hook = passthrough_post_auth_hook
c.KubeSpawner.apply_user_options = drop_stale_user_options

if os.environ["POD_NAMESPACE"] == "cms":
    c.KubeSpawner.environment.setdefault(
        "DASK_GATEWAY__ADDRESS", "http://dask-gateway-k8s.geddes.rcac.purdue.edu"
    )
    c.KubeSpawner.environment.setdefault(
        "DASK_GATEWAY__PROXY_ADDRESS",
        "traefik-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8786",
    )
