import asyncio
import json
import os
import re
from typing import Any

from jupyterhub import orm
from kubernetes_asyncio.client import V1ConfigMap, V1ObjectMeta
from kubernetes_asyncio.client.rest import ApiException
from ldap3 import BASE, SUBTREE, Connection, Server

# JupyterHub injects `c` at exec time; the annotation is for type checkers only.
c: Any

# AF_LDAP_* are set only by the e2e harness (tests/e2e_hub).
LDAP_HOST = os.environ.get("AF_LDAP_HOST", "geddes-auth.rcac.purdue.edu")
LDAP_TLS = os.environ.get("AF_LDAP_TLS", "true").lower() != "false"
BASE_DN = "ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"
ATTRS = ["uidNumber", "gidNumber"]

# The suffixes custom-spawner.py gives accounts from outside Purdue.
EXTERNAL_SUFFIXES = ("-cern", "-fnal")
# The pooled accounts LDAP holds.
POOL = [f"paf{n:04d}" for n in range(400)]
# The ConfigMap recording which one each such user has (README.md).
LEDGER = "af-pooled-accounts"
LEDGER_ATTEMPTS = 5
# What the API server accepts as a ConfigMap key.
LEDGER_KEY = re.compile(r"[-._a-zA-Z0-9]{1,253}")


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


def is_external(username: str) -> bool:
    return username.endswith(EXTERNAL_SUFFIXES)


def seed_ledger(users: list[tuple[int, str]]) -> dict[str, str]:
    """The ledger a Hub without one starts from, given its (id, name) users.

    Every external user keeps the account of their hub id, and every hub id up
    to the highest that belongs to no user is reserved under `deleted-<id>`:
    that account may still own files of a user deleted before the ledger.
    """
    ids = {user_id for user_id, _ in users}
    data = {
        name: POOL[user_id]
        for user_id, name in users
        if is_external(name) and user_id < len(POOL) and LEDGER_KEY.fullmatch(name)
    }
    for gap in range(1, min(max(ids, default=0) + 1, len(POOL))):
        if gap not in ids:
            data[f"deleted-{gap}"] = POOL[gap]
    return data


def free_account(ledger: dict[str, str], user_id: int) -> str:
    """The account of the user's hub id while no entry holds it, else the highest free.

    The low accounts may own files of users deleted before the ledger, so the
    never-used top of the pool goes first.
    """
    used = set(ledger.values())
    if user_id < len(POOL) and POOL[user_id] not in used:
        return POOL[user_id]
    for account in reversed(POOL):
        if account not in used:
            return account
    raise RuntimeError("ran out of pooled accounts for external users")


async def read_ledger(spawner: Any) -> Any:
    """The ledger ConfigMap, created from the Hub's users if it does not exist."""
    api, namespace = spawner.api, spawner.namespace
    timeout = spawner.k8s_api_request_timeout
    try:
        return await api.read_namespaced_config_map(
            LEDGER, namespace, _request_timeout=timeout
        )
    except ApiException as e:
        if e.status != 404:
            raise
    rows = spawner.user.db.query(orm.User.id, orm.User.name).all()
    users = [(int(i), str(n)) for i, n in rows]
    seed = V1ConfigMap(metadata=V1ObjectMeta(name=LEDGER), data=seed_ledger(users))
    spawner.log.info("Creating %s from %d hub users", LEDGER, len(users))
    for _, name in users:
        if is_external(name) and not LEDGER_KEY.fullmatch(name):
            spawner.log.warning("%s: %r is no ConfigMap key, left out", LEDGER, name)
    try:
        return await api.create_namespaced_config_map(
            namespace, seed, _request_timeout=timeout
        )
    except ApiException as e:
        if e.status != 409:
            raise
    return await api.read_namespaced_config_map(
        LEDGER, namespace, _request_timeout=timeout
    )


async def pooled_account(spawner: Any, username: str) -> str:
    """The account `username` runs as, allocated on first use."""
    if not LEDGER_KEY.fullmatch(username):
        raise RuntimeError(f"{username!r} cannot be a key of {LEDGER}")
    for _ in range(LEDGER_ATTEMPTS):
        ledger = await read_ledger(spawner)
        data: dict[str, str] = dict(ledger.data or {})
        if username in data:
            return data[username]
        data[username] = free_account(data, int(spawner.user.id))
        ledger.data = data
        try:
            await spawner.api.replace_namespaced_config_map(
                LEDGER,
                spawner.namespace,
                ledger,
                _request_timeout=spawner.k8s_api_request_timeout,
            )
        except ApiException as e:
            if e.status != 409:
                raise
            continue
        spawner.log.info("Recorded %s for %s in %s", data[username], username, LEDGER)
        return data[username]
    raise RuntimeError(f"{LEDGER} kept changing while allocating for {username}")


async def passthrough_auth_state_hook(spawner: Any, auth_state: Any) -> None:
    spawner.userdata = {"name": auth_state["name"], "domain": auth_state["domain"]}
    domain = spawner.userdata["domain"]
    username = spawner.userdata["name"]
    spawner.environment["NB_USER"] = username

    if domain != "purdue.edu":
        username = await pooled_account(spawner, username)

    # ldap3 is synchronous; off-load it so a slow directory does not stall the Hub.
    uid, gid = await asyncio.to_thread(ldap_lookup, username)
    spawner.environment["NB_UID"] = str(uid)
    spawner.environment["NB_GID"] = str(gid)

    # pixi may create layout under $PIXI_HOME; /opt/pixi stays read-only.
    spawner.environment["PIXI_HOME"] = (
        f"/work/users/{spawner.environment['NB_USER']}/.pixi-home"
    )


c.KubeSpawner.auth_state_hook = passthrough_auth_state_hook
c.KubeSpawner.notebook_dir = "~"
c.KubeSpawner.working_dir = "/home/{username}"
c.KubeSpawner.disable_user_config = True
c.KubeSpawner.http_timeout = 600
c.KubeSpawner.start_timeout = 600
