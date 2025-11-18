import os

from oauthenticator.cilogon import CILogonOAuthenticator
from tornado import web


class PurdueCILogonOAuthenticator(CILogonOAuthenticator):
    async def authenticate(self, handler, data=None):
        import pprint

        ret = await super().authenticate(handler, data)
        print("in auth:")
        pprint.pprint(ret)
        username, domain = ret["auth_state"]["cilogon_user"]["eppn"].split("@")
        fixedUsername = None

        if domain == "purdue.edu":
            fixedUsername = username
            with open("/etc/secrets/af-auth-purdue/userlist") as file:
                if not f"{username}\n" in file.readlines():
                    raise web.HTTPError(
                        500,
                        f"Access denied! User {username} is not in the list of authorized users.",
                    )

        elif domain == "cern.ch":
            fixedUsername = username + "-cern"
            with open("/etc/secrets/af-auth-cern/userlist") as file:
                if not f"{username}\n" in file.readlines():
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


def passthrough_post_auth_hook(authenticator, handler, authentication):
    import pprint

    print("in post auth:")
    pprint.pprint(authentication)
    if authentication["auth_state"] is None:
        authentication["auth_state"] = {}
    authentication["auth_state"]["name"] = authentication["name"]
    authentication["auth_state"]["domain"] = authentication["domain"]
    return authentication


c.JupyterHub.authenticator_class = PurdueCILogonOAuthenticator
c.PurdueCILogonOAuthenticator.post_auth_hook = passthrough_post_auth_hook

# Disable automatic service environment variable injection for all user pods
# This prevents unrelated service environment variables from being injected
c.KubeSpawner.extra_pod_config = {
    "enableServiceLinks": False
}

if os.environ["POD_NAMESPACE"] == "cms":
    # c.KubeSpawner.service_account = "dask-sa"
    # c.KubeSpawner.automount_service_account_token = True
    # The current environment and dask configuration via environment
    # export DASK_DISTRIBUTED__DASHBOARD_LINK=/user/$NB_USER/proxy/8787/status
    # export DASK_GATEWAY__AUTH__TYPE=jupyterhub
    # export DASK_GATEWAY__CLUSTER__OPTIONS__IMAGE={JUPYTER_IMAGE_SPEC}
    # export DASK_GATEWAY__PUBLIC_ADDRESS=/services/dask-gateway/
    # export DASK_ROOT_CONFIG=/opt/conda/etc
    c.KubeSpawner.environment.setdefault(
        "DASK_GATEWAY__ADDRESS", "http://dask-gateway-k8s-slurm.geddes.rcac.purdue.edu"
    )
    c.KubeSpawner.environment.setdefault(
        "DASK_GATEWAY__PROXY_ADDRESS",
        "api-dask-gateway-k8s-slurm.cms.geddes.rcac.purdue.edu:8000",
    )


c.KubeSpawner.environment.setdefault(
    "DASK_LABEXTENSION__FACTORY__MODULE", "dask_gateway"
)
c.KubeSpawner.environment.setdefault(
    "DASK_LABEXTENSION__FACTORY__CLASS", "GatewayCluster"
)
c.KubeSpawner.environment.setdefault(
    "DASK_LABEXTENSION__FACTORY__KWARGS__ADDRESS",
    "http://dask-gateway-k8s-slurm.geddes.rcac.purdue.edu",
)
c.KubeSpawner.environment.setdefault(
    "DASK_LABEXTENSION__FACTORY__KWARGS__PROXY_ADDRESS",
    "api-dask-gateway-k8s-slurm.cms.geddes.rcac.purdue.edu:8000",
)
c.KubeSpawner.environment.setdefault(
    "DASK_LABEXTENSION__FACTORY__KWARGS__PUBLIC_ADDRESS",
    "https://dask-gateway-k8s-slurm.geddes.rcac.purdue.edu",
)

# if ("-cern" in os.environ["NB_USER"]) or ("-fnal" in os.environ["NB_USER"]):
#     c.KubeSpawner.environment.setdefault("DASK_LABEXTENSION__FACTORY__KWARGS__ADDRESS", "http://dask-gateway-k8s.geddes.rcac.purdue.edu")
#     c.KubeSpawner.environment.setdefault("DASK_LABEXTENSION__FACTORY__KWARGS__PROXY_ADDRESS", "api-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8000")
#     c.KubeSpawner.environment.setdefault("DASK_LABEXTENSION__FACTORY__KWARGS__PUBLIC_ADDRESS", "https://dask-gateway-k8s.geddes.rcac.purdue.edu")
