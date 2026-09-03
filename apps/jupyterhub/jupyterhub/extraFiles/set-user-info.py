import json
import os
from typing import Any

from ldap3 import SUBTREE, Connection, Server

# `c` is the traitlets config object JupyterHub injects into this file's
# globals at exec time. A bare annotation declares its type for static
# checkers without creating (or shadowing) the runtime binding.
c: Any


def ldap_lookup(username: str) -> tuple[Any, Any]:
    # AF_LDAP_* are only set by the e2e harness (tests/e2e_hub), which points
    # at a plaintext mock; unset (production) keeps the geddes-auth TLS
    # path byte-for-byte.
    url = os.environ.get("AF_LDAP_HOST", "geddes-auth.rcac.purdue.edu")
    use_tls = os.environ.get("AF_LDAP_TLS", "true").lower() != "false"
    baseDN = "ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"
    search_filter = "(uid={0}*)"
    attrs = ["uidNumber", "gidNumber"]
    s = Server(host=url, use_ssl=use_tls, get_info="ALL")
    conn = Connection(s, version=3, authentication="ANONYMOUS")
    if use_tls:
        conn.start_tls()
    else:
        conn.bind()
    conn.search(
        search_base=baseDN,
        search_filter=search_filter.format(username),
        search_scope=SUBTREE,
        attributes=attrs,
    )
    ldap_result_id = json.loads(conn.response_to_json())
    print(ldap_result_id)
    result = ldap_result_id["entries"][0]["attributes"]
    uid_number = result["uidNumber"]
    gid_number = result["gidNumber"]
    print("UID", +uid_number)
    print("GID", +gid_number)
    return uid_number, gid_number


def passthrough_auth_state_hook(spawner: Any, auth_state: Any) -> None:
    print("auth_state", auth_state)
    spawner.userdata = {"name": auth_state["name"], "domain": auth_state["domain"]}
    domain = spawner.userdata["domain"]
    username = spawner.userdata["name"]
    spawner.environment["NB_USER"] = username

    if domain == "purdue.edu":
        uid, gid = ldap_lookup(username)
        spawner.environment["NB_UID"] = str(uid)
        spawner.environment["NB_GID"] = str(gid)
    else:
        # External users map onto a pooled paf#### account via hub user id.
        # paf0000–paf0399 are provisioned in LDAP; beyond that there is no
        # account to map onto — refuse the spawn rather than falling back to
        # a shared UID or looking up a nonexistent paf04xx entry.
        af_id = int(spawner.user.id)
        if af_id > 399:
            raise RuntimeError(
                f"ran out of accounts for external users (AF ID {af_id})"
            )
        username = "paf{:04d}".format(af_id)
        uid, gid = ldap_lookup(username)
        spawner.environment["NB_UID"] = str(uid)
        spawner.environment["NB_GID"] = str(gid)

    # Pixi CLI and pixi-kernel run `pixi info`, which may create $PIXI_HOME/envs and
    # other layout. Keep /opt/pixi read-only; store per-user Pixi state on /work.
    spawner.environment["PIXI_HOME"] = (
        f"/work/users/{spawner.environment['NB_USER']}/.pixi-home"
    )


c.KubeSpawner.auth_state_hook = passthrough_auth_state_hook
c.KubeSpawner.notebook_dir = "~"
c.KubeSpawner.working_dir = "/home/{username}"
c.KubeSpawner.disable_user_config = True
c.KubeSpawner.http_timeout = 600
c.KubeSpawner.start_timeout = 600
c.JupyterHub.authenticate_prometheus = False
