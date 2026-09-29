"""Ray Jobs gateway: each AF user's `ray job` calls reach a Ray cluster of their own.

A session's RAY_AUTH_TOKEN is its JupyterHub token, which the Ray CLI sends as
a bearer token. The Hub says whose it is, and the call goes on to that user's
RayCluster: created from raycluster.yaml when they first send code or a job,
running as them, and deleted once idle. Only the gateway holds a cluster's
own Ray token. A cluster runs the global Pixi environment, or the one the
session's `ray` command names in the X-AF-Ray-Env header.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import hmac
import json
import logging
import os
import posixpath
import re
import ssl
import time
import urllib.parse
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from aiohttp import (
    ClientError,
    ClientSession,
    ClientTimeout,
    TCPConnector,
    WSMsgType,
    web,
)

log = logging.getLogger("ray-train-gateway")

NAMESPACE = os.environ.get("NAMESPACE", "cms")
HUB_API = os.environ.get("JUPYTERHUB_API_URL", "http://hub:8081/hub/api")
TEMPLATE = Path(os.environ.get("RAY_CLUSTER_TEMPLATE", "/app/raycluster.yaml"))
IDLE_TIMEOUT_S = float(os.environ.get("IDLE_TIMEOUT_S", "900"))
START_TIMEOUT_S = float(os.environ.get("START_TIMEOUT_S", "600"))
START_POLL_S = 5.0
REAP_EVERY_S = 60.0
# This service's own Hub token, from the `hub` Secret: the file appears once the Hub registers the service.
SERVICE_TOKEN_FILE = Path("/etc/hub-token/token")
USER_CACHE_S = 300.0
LDAP_HOST = "geddes-auth.rcac.purdue.edu"
LDAP_BASE = "ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"
KUBE_API = "https://kubernetes.default.svc"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
RAYCLUSTERS = f"/apis/ray.io/v1/namespaces/{NAMESPACE}/rayclusters"
SECRETS = f"/api/v1/namespaces/{NAMESPACE}/secrets"
MANAGED_BY = "ray-train-gateway"
# Set by the session's `ray` command (docker/purdue-af/ray-wrapper).
ENV_HEADER = "X-AF-Ray-Env"
ENV_ANNOTATION = "purdue-af/ray-env"
# The environment pixi-global-sync keeps (apps/af-utils/pixi-global-sync).
DEFAULT_ENV = "/work/pixi/global/.pixi/envs/default"
# singleuser.podNameTemplate in the Hub values
SESSION_POD = re.compile(r"purdue-af-(\d+)")
# custom-spawner.py names the accounts from outside Purdue <login>-cern and <login>-fnal
EXTERNAL_SUFFIXES = ("-cern", "-fnal")
# The calls `ray job` makes; nothing else is forwarded.
JOBS_API = re.compile(r"/api/(version|jobs/.*|packages/[^/]+/[^/]+)")
# CURRENT_VERSION in ray/dashboard/modules/version.py
JOBS_API_VERSION = "4"
LIVE_STATUSES = {"PENDING", "RUNNING"}


@dataclass(frozen=True)
class User:
    name: str
    af_id: int
    account: str
    uid: int
    gid: int

    @property
    def cluster(self) -> str:
        return f"ray-train-{self.af_id}"


def bearer_token(header: str) -> str | None:
    scheme, _, token = header.partition(" ")
    token = token.strip()
    return token if scheme.lower() in ("bearer", "token") and token else None


def session_af_id(user_model: dict[str, Any]) -> int | None:
    """The user's AF id, from the pod name of their running session."""
    server = (user_model.get("servers") or {}).get("") or {}
    pod_name = (server.get("state") or {}).get("pod_name", "")
    match = SESSION_POD.fullmatch(pod_name)
    return int(match[1]) if match else None


def ldap_account(username: str, af_id: int) -> str:
    """The account a session runs as (set-user-info.py): pooled paf#### outside Purdue."""
    if not username.endswith(EXTERNAL_SUFFIXES):
        return username
    if af_id > 399:
        raise web.HTTPForbidden(text=f"There is no pooled account for AF user {af_id}.")
    return f"paf{af_id:04d}"


def ldap_ids(account: str) -> tuple[int, int]:
    """UID/GID of an account, read by its DN as set-user-info.py does."""
    from ldap3 import BASE, Connection, Server

    conn = Connection(
        Server(host=LDAP_HOST, use_ssl=True, get_info="ALL"),
        version=3,
        authentication="ANONYMOUS",
    )
    conn.start_tls()
    conn.search(
        search_base=f"uid={account},{LDAP_BASE}",
        search_filter="(objectClass=*)",
        search_scope=BASE,
        attributes=["uidNumber", "gidNumber"],
    )
    entries = json.loads(conn.response_to_json())["entries"]
    if not entries:
        raise web.HTTPForbidden(text=f"There is no LDAP account {account}.")
    found = entries[0]["attributes"]
    return int(found["uidNumber"]), int(found["gidNumber"])


def cluster_token(key: str, cluster: str) -> str:
    """Derived rather than stored, so the gateway never reads a Secret."""
    return hmac.new(key.encode(), cluster.encode(), hashlib.sha256).hexdigest()


def head_url(cluster: str) -> str:
    return f"http://{cluster}-head-svc.{NAMESPACE}.svc.cluster.local:8265"


def build_cluster(template: dict[str, Any], user: User, env: str) -> dict[str, Any]:
    cluster = copy.deepcopy(template)
    metadata = cluster["metadata"]
    metadata["name"] = user.cluster
    metadata.setdefault("labels", {})["app.kubernetes.io/managed-by"] = MANAGED_BY
    metadata.setdefault("annotations", {})[ENV_ANNOTATION] = env
    cluster["spec"]["authOptions"]["secretName"] = user.cluster
    pod = cluster["spec"]["headGroupSpec"]["template"]["spec"]
    pod.setdefault("securityContext", {}).update(
        {"runAsUser": user.uid, "runAsGroup": user.gid}
    )
    for container in pod["containers"]:
        variables = container.setdefault("env", [])
        # `ray start`, and with it every Ray process, then come from the environment.
        for variable in variables:
            if variable["name"] == "PATH":
                variable["value"] = f"{env}/bin:{variable['value']}"
        variables.append({"name": "CONDA_PREFIX", "value": env})
        variables.append({"name": "USER", "value": user.account})
    return cluster


def token_secret(user: User, owner_uid: str, token: str) -> dict[str, Any]:
    """KubeRay reads the cluster's token from it; it goes when the cluster goes."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": user.cluster,
            "labels": {"app.kubernetes.io/managed-by": MANAGED_BY},
            "ownerReferences": [
                {
                    "apiVersion": "ray.io/v1",
                    "kind": "RayCluster",
                    "name": user.cluster,
                    "uid": owner_uid,
                }
            ],
        },
        "stringData": {"auth_token": token},
    }


def starts_cluster(method: str, path: str) -> bool:
    """Sending code or a job starts the user's cluster; every other call only reads it."""
    return path.startswith("/api/packages/") or (
        method == "POST" and path == "/api/jobs/"
    )


def head_ready(cluster: dict[str, Any]) -> bool:
    conditions = (cluster.get("status") or {}).get("conditions") or []
    return any(
        c.get("type") == "HeadPodReady" and c.get("status") == "True"
        for c in conditions
    )


def load_template() -> dict[str, Any]:
    template: dict[str, Any] = yaml.safe_load(TEMPLATE.read_text())
    return template


def ray_version() -> str:
    return str(load_template()["spec"]["rayVersion"])


def env_roots(template: dict[str, Any]) -> list[str]:
    """Where an environment may live: the storage a cluster mounts."""
    pod = template["spec"]["headGroupSpec"]["template"]["spec"]
    scratch = {v["name"] for v in pod["volumes"] if "emptyDir" in v}
    (container,) = pod["containers"]
    return [
        m["mountPath"] for m in container["volumeMounts"] if m["name"] not in scratch
    ]


def requested_env(header: str, template: dict[str, Any]) -> str:
    """The environment a call asks for, or the global one."""
    if not header:
        return DEFAULT_ENV
    path = posixpath.normpath(header)
    roots = env_roots(template)
    if not header.startswith("/") or not any(
        path == root or path.startswith(root.rstrip("/") + "/") for root in roots
    ):
        raise web.HTTPBadRequest(
            text=f"{header} is not on storage your Ray cluster mounts: {', '.join(roots)}."
        )
    return path


def cluster_env(cluster: dict[str, Any]) -> str:
    return str((cluster["metadata"].get("annotations") or {}).get(ENV_ANNOTATION, ""))


def runs(cluster: dict[str, Any], env: str) -> bool:
    """Whether the cluster stays up and runs `env`."""
    return (
        not cluster["metadata"].get("deletionTimestamp") and cluster_env(cluster) == env
    )


class Gateway:
    def __init__(
        self, http: ClientSession, kube: ClientSession, token_file: Path
    ) -> None:
        self.http = http
        self.kube_http = kube
        self.token_file = token_file
        self.last_used: dict[str, float] = {}
        self._users: dict[str, tuple[float, User]] = {}

    @property
    def service_token(self) -> str:
        """Reads session pod names, and keys the cluster tokens; empty until the Hub makes it."""
        try:
            return self.token_file.read_text().strip()
        except FileNotFoundError:
            return ""

    def cluster_auth(self, cluster: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {cluster_token(self.service_token, cluster)}"}

    async def user_for(self, token: str) -> User:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        cached = self._users.get(key)
        if cached and cached[0] > now:
            return cached[1]
        name = await self.whoami(token)
        af_id = session_af_id(await self.hub_user(name))
        if af_id is None:
            raise web.HTTPConflict(
                text="Start your AF session first: Ray jobs run as the user of a running session."
            )
        account = ldap_account(name, af_id)
        uid, gid = await asyncio.to_thread(ldap_ids, account)
        user = User(name=name, af_id=af_id, account=account, uid=uid, gid=gid)
        self._users = {k: v for k, v in self._users.items() if v[0] > now}
        self._users[key] = (now + USER_CACHE_S, user)
        return user

    async def whoami(self, token: str) -> str:
        async with self.http.get(
            f"{HUB_API}/user", headers={"Authorization": f"token {token}"}
        ) as r:
            if r.status in (401, 403):
                raise web.HTTPUnauthorized(
                    text="JupyterHub did not accept RAY_AUTH_TOKEN: it must be this session's JUPYTERHUB_API_TOKEN."
                )
            if r.status != 200:
                raise web.HTTPBadGateway(text=f"JupyterHub answered HTTP {r.status}.")
            model: dict[str, Any] = await r.json()
        return str(model["name"])

    async def hub_user(self, name: str) -> dict[str, Any]:
        async with self.http.get(
            f"{HUB_API}/users/{urllib.parse.quote(name, safe='')}",
            headers={"Authorization": f"token {self.service_token}"},
        ) as r:
            if r.status != 200:
                raise web.HTTPBadGateway(
                    text=f"JupyterHub answered HTTP {r.status} for the user's session."
                )
            model: dict[str, Any] = await r.json()
        return model

    async def kube(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        token = (SERVICE_ACCOUNT / "token").read_text().strip()
        async with self.kube_http.request(
            method,
            KUBE_API + path,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        ) as r:
            data: dict[str, Any] = await r.json(content_type=None)
            return r.status, data

    async def cluster(self, name: str) -> dict[str, Any] | None:
        status, body = await self.kube("GET", f"{RAYCLUSTERS}/{name}")
        if status == 404:
            return None
        if status != 200:
            raise web.HTTPBadGateway(
                text=f"Kubernetes answered HTTP {status} for the Ray cluster."
            )
        return body

    async def start_cluster(self, user: User, env: str) -> None:
        """Create the user's cluster for `env` unless it runs it; return once its head answers."""
        cluster = await self.cluster(user.cluster)
        if cluster is not None and not runs(cluster, env):
            if not cluster["metadata"].get("deletionTimestamp"):
                if await self.busy(cluster):
                    raise web.HTTPConflict(
                        text="Your Ray cluster runs another environment and has a job pending or running: wait for it, or stop it."
                    )
                await self.delete(user.cluster)
            await self.wait_until_gone(user.cluster)
            cluster = None
        if cluster is None:
            status, created = await self.kube(
                "POST", RAYCLUSTERS, build_cluster(load_template(), user, env)
            )
            if status == 201:
                log.info("created %s", user.cluster)
                cluster = created
            elif status == 409:
                # Another call created it first.
                cluster = await self.cluster(user.cluster)
            else:
                raise web.HTTPBadGateway(
                    text=f"Could not create your Ray cluster: {created.get('message', status)}"
                )
        if cluster is None or not runs(cluster, env):
            raise web.HTTPConflict(
                text="Another call is replacing your Ray cluster; submit again in a minute."
            )
        secret = token_secret(
            user,
            cluster["metadata"]["uid"],
            cluster_token(self.service_token, user.cluster),
        )
        deadline = time.monotonic() + START_TIMEOUT_S
        while True:
            # Again on every pass: a Secret left by a deleted namesake is collected with it.
            status, body = await self.kube("POST", SECRETS, secret)
            if status not in (201, 409):
                raise web.HTTPBadGateway(
                    text=f"Could not create your Ray cluster's token: {body.get('message', status)}"
                )
            if await self.head_status(user.cluster) == 200:
                break
            if time.monotonic() > deadline:
                raise web.HTTPServiceUnavailable(
                    text="Your Ray cluster did not start in time: no T4 may be free. Try again later."
                )
            await asyncio.sleep(START_POLL_S)
        # A Ray that ignores RAY_AUTH_MODE would serve anyone who reaches it.
        if await self.head_status(user.cluster, authorized=False) == 200:
            await self.delete(user.cluster)
            raise web.HTTPBadRequest(
                text="The Ray in that environment does not enforce the cluster's token."
            )

    async def delete(self, name: str) -> None:
        await self.kube(
            "DELETE", f"{RAYCLUSTERS}/{name}", {"propagationPolicy": "Background"}
        )

    async def wait_until_gone(self, name: str) -> None:
        deadline = time.monotonic() + START_TIMEOUT_S
        while await self.cluster(name) is not None:
            if time.monotonic() > deadline:
                raise web.HTTPServiceUnavailable(
                    text="Your previous Ray cluster is still shutting down; submit again in a minute."
                )
            await asyncio.sleep(START_POLL_S)

    async def head_status(self, cluster: str, authorized: bool = True) -> int | None:
        try:
            async with self.http.get(
                head_url(cluster) + "/api/version",
                headers=self.cluster_auth(cluster) if authorized else {},
                timeout=ClientTimeout(total=5),
            ) as r:
                return r.status
        except (ClientError, asyncio.TimeoutError):
            return None

    async def forward(self, request: web.Request, cluster: str) -> web.StreamResponse:
        url = head_url(cluster) + request.rel_url.path_qs
        headers = self.cluster_auth(cluster)
        if "Content-Type" in request.headers:
            headers["Content-Type"] = request.headers["Content-Type"]
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.forward_websocket(request, url, headers)
        body = request.content.iter_any() if request.body_exists else None
        try:
            upstream = await self.http.request(
                request.method,
                url,
                headers=headers,
                data=body,
                timeout=ClientTimeout(total=None, sock_connect=10),
            )
        except ClientError as e:
            raise web.HTTPBadGateway(
                text=f"Your Ray cluster did not answer: {e}"
            ) from e
        async with upstream:
            response = web.StreamResponse(status=upstream.status)
            if "Content-Type" in upstream.headers:
                response.headers["Content-Type"] = upstream.headers["Content-Type"]
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
        return response

    async def forward_websocket(
        self, request: web.Request, url: str, headers: dict[str, str]
    ) -> web.WebSocketResponse:
        client = web.WebSocketResponse()
        await client.prepare(request)
        try:
            async with self.http.ws_connect(
                "ws" + url.removeprefix("http"), headers=headers
            ) as upstream:
                async for message in upstream:
                    if message.type == WSMsgType.TEXT:
                        await client.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await client.send_bytes(message.data)
        except (ClientError, ConnectionResetError):
            pass
        await client.close()
        return client

    async def reap_idle(self) -> None:
        status, listing = await self.kube(
            "GET",
            f"{RAYCLUSTERS}?labelSelector=app.kubernetes.io%2Fmanaged-by%3D{MANAGED_BY}",
        )
        if status != 200:
            log.warning("listing Ray clusters: HTTP %s", status)
            return
        now = time.monotonic()
        for cluster in listing.get("items", []):
            name = cluster["metadata"]["name"]
            # A cluster first seen here (the gateway restarted) starts its idle clock now.
            if now - self.last_used.setdefault(name, now) < IDLE_TIMEOUT_S:
                continue
            if await self.busy(cluster):
                self.last_used[name] = now
                continue
            await self.delete(name)
            self.last_used.pop(name, None)
            log.info("deleted idle %s", name)

    async def busy(self, cluster: dict[str, Any]) -> bool:
        """A pending or running job keeps a cluster; so does a ready head that did not answer."""
        name = cluster["metadata"]["name"]
        try:
            async with self.http.get(
                head_url(name) + "/api/jobs/",
                headers=self.cluster_auth(name),
                timeout=ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    jobs: list[dict[str, Any]] = await r.json()
                    return any(job.get("status") in LIVE_STATUSES for job in jobs)
                if r.status in (401, 403):
                    # Made under another service token: nothing can reach it any more.
                    return False
        except (ClientError, asyncio.TimeoutError):
            pass
        return head_ready(cluster)


GATEWAY = web.AppKey("gateway", Gateway)


async def handle(request: web.Request) -> web.StreamResponse:
    if request.path == "/healthz":
        return web.Response(text="ok")
    if not JOBS_API.fullmatch(request.path):
        raise web.HTTPNotFound(text="This address serves the Ray Jobs API only.")
    gateway = request.app[GATEWAY]
    if not gateway.service_token:
        raise web.HTTPServiceUnavailable(
            text="Ray Train is not enabled: JupyterHub has not registered the ray-train-gateway service."
        )
    token = bearer_token(request.headers.get("Authorization", ""))
    if token is None:
        raise web.HTTPUnauthorized(
            text="No token: RAY_AUTH_MODE=token and RAY_AUTH_TOKEN=$JUPYTERHUB_API_TOKEN must be set."
        )
    user = await gateway.user_for(token)
    if request.path == "/api/version":
        return web.json_response(
            {
                "version": JOBS_API_VERSION,
                "ray_version": ray_version(),
                "ray_commit": "",
                "session_name": user.cluster,
            }
        )
    gateway.last_used[user.cluster] = time.monotonic()
    if starts_cluster(request.method, request.path):
        env = requested_env(request.headers.get(ENV_HEADER, ""), load_template())
        await gateway.start_cluster(user, env)
    elif await gateway.cluster(user.cluster) is None:
        if request.method == "GET" and request.path == "/api/jobs/":
            return web.json_response([])
        raise web.HTTPNotFound(
            text="You have no Ray cluster now: an idle one is removed, with its job history."
        )
    return await gateway.forward(request, user.cluster)


async def reap_forever(gateway: Gateway) -> None:
    while True:
        await asyncio.sleep(REAP_EVERY_S)
        try:
            await gateway.reap_idle()
        except Exception:
            log.exception("reaping idle Ray clusters")


async def lifecycle(app: web.Application) -> AsyncIterator[None]:
    kube_tls = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    async with (
        ClientSession() as http,
        ClientSession(connector=TCPConnector(ssl=kube_tls)) as kube,
    ):
        gateway = Gateway(http, kube, SERVICE_TOKEN_FILE)
        app[GATEWAY] = gateway
        reaper = asyncio.create_task(reap_forever(gateway))
        yield
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper


def make_app() -> web.Application:
    app = web.Application()
    app.cleanup_ctx.append(lifecycle)
    app.router.add_route("*", "/{path:.*}", handle)
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    web.run_app(make_app(), port=8265, access_log=None)
