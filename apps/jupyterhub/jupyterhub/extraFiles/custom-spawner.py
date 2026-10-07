import os
from typing import Any

from jupyterhub.utils import maybe_future
from oauthenticator.cilogon import CILogonOAuthenticator
from tornado import web

# JupyterHub injects `c` at exec time; the annotation is for type checkers only.
c: Any


# Home domain of an eppn -> suffix of the AF username.
SUFFIXES = {"purdue.edu": "", "cern.ch": "-cern", "fnal.gov": "-fnal"}
USERLISTS = {
    "purdue.edu": (
        "/etc/secrets/af-auth-purdue/userlist",
        "Access denied! User {username} is not in the list of authorized users.",
    ),
    "cern.ch": (
        "/etc/secrets/af-auth-cern/userlist",
        "Access denied! Only CMS members are allowed to log in with CERN credentials.",
    ),
}


class PurdueCILogonOAuthenticator(CILogonOAuthenticator):
    def _af_identity(self, user_info: Any) -> tuple[str, str]:
        """The eppn split into its local part and a domain the AF knows."""
        eppn = super().user_info_to_username(user_info)
        username, _, domain = eppn.partition("@")
        if domain not in SUFFIXES:
            raise web.HTTPError(403, "Failed to get username from CILogon")
        return username, domain

    def user_info_to_username(self, user_info: Any) -> str:
        username, domain = self._af_identity(user_info)
        # Denied here: Authenticator.allow_all skips check_allowed.
        if domain in USERLISTS:
            path, message = USERLISTS[domain]
            with open(path) as file:
                if f"{username}\n" not in file.readlines():
                    raise web.HTTPError(403, message.format(username=username))
        return username + SUFFIXES[domain]

    def build_auth_state_dict(self, token_info: Any, user_info: Any) -> Any:
        auth_state = super().build_auth_state_dict(token_info, user_info)
        username, domain = self._af_identity(user_info)
        # set-user-info.py reads both.
        auth_state["name"] = self.normalize_username(username + SUFFIXES[domain])
        auth_state["domain"] = domain
        return auth_state

    async def refresh_user(self, user: Any, handler: Any = None, **kwargs: Any) -> Any:
        # No refresh against CILogon: the identity from login stands.
        return True


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
c.KubeSpawner.apply_user_options = drop_stale_user_options

if os.environ["POD_NAMESPACE"] == "cms":
    c.KubeSpawner.environment.setdefault(
        "DASK_GATEWAY__ADDRESS", "http://dask-gateway.geddes.rcac.purdue.edu"
    )
    c.KubeSpawner.environment.setdefault(
        "DASK_GATEWAY__PROXY_ADDRESS",
        "traefik-dask-gateway.cms.geddes.rcac.purdue.edu:8786",
    )
    c.KubeSpawner.environment.setdefault("DASK_GATEWAY__AUTH__TYPE", "jupyterhub")
