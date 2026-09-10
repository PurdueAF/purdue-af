import asyncio
import json
import os
from typing import Any

from ldap3 import BASE, SUBTREE, Connection, Server

# `c` is the traitlets config object JupyterHub injects into this file's
# globals at exec time. A bare annotation declares its type for static
# checkers without creating (or shadowing) the runtime binding.
c: Any

# AF_LDAP_* are only set by the e2e harness (tests/e2e_hub), which points
# at a plaintext mock; unset (production) keeps the geddes-auth TLS
# path byte-for-byte.
LDAP_HOST = os.environ.get("AF_LDAP_HOST", "geddes-auth.rcac.purdue.edu")
LDAP_TLS = os.environ.get("AF_LDAP_TLS", "true").lower() != "false"
BASE_DN = "ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"
ATTRS = ["uidNumber", "gidNumber"]


def _connect() -> Any:
    s = Server(host=LDAP_HOST, use_ssl=LDAP_TLS, get_info="ALL")
    conn = Connection(s, version=3, authentication="ANONYMOUS")
    if LDAP_TLS:
        conn.start_tls()
    else:
        conn.bind()
    return conn


def _entry(conn: Any) -> dict[str, Any] | None:
    entries = json.loads(conn.response_to_json())["entries"]
    return entries[0]["attributes"] if entries else None


def ldap_lookup(username: str) -> tuple[Any, Any]:
    """UID/GID for `username`, read by DN.

    geddes-auth carries no index on uid: any filtered search under BASE_DN
    scans the whole tree and takes 15-25s, and because this runs inline on
    the Hub's event loop it froze every request for that long (2026-09-03
    migration off geddes-aux). Accounts live at uid=<name>,<BASE_DN>, so a
    base-scope read of that DN returns the same attributes in ~0ms. The
    search stays as a fallback for any entry that is not at its own DN.
    """
    conn = _connect()
    conn.search(
        search_base=f"uid={username},{BASE_DN}",
        search_filter="(objectClass=*)",
        search_scope=BASE,
        attributes=ATTRS,
    )
    result = _entry(conn)
    if result is None:
        print(f"LDAP: no entry at uid={username},{BASE_DN}; falling back to search")
        conn.search(
            search_base=BASE_DN,
            search_filter=f"(uid={username}*)",
            search_scope=SUBTREE,
            attributes=ATTRS,
        )
        result = _entry(conn)
    if result is None:
        raise RuntimeError(f"no LDAP entry for {username}")
    uid_number = result["uidNumber"]
    gid_number = result["gidNumber"]
    print("UID", +uid_number)
    print("GID", +gid_number)
    return uid_number, gid_number


async def passthrough_auth_state_hook(spawner: Any, auth_state: Any) -> None:
    spawner.userdata = {"name": auth_state["name"], "domain": auth_state["domain"]}
    domain = spawner.userdata["domain"]
    username = spawner.userdata["name"]
    spawner.environment["NB_USER"] = username

    if domain != "purdue.edu":
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

    # ldap3 is synchronous; off-loading keeps a slow directory from stalling
    # the Hub for every other user, as the pre-DN-read lookups did.
    uid, gid = await asyncio.to_thread(ldap_lookup, username)
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
