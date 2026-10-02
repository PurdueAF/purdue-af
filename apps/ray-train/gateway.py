"""Ray Client gateway: each AF user's `ray.init("ray://ray-train-gateway:10001")`
reaches a Ray cluster of their own.

A session's RAY_AUTH_TOKEN is its JupyterHub token, which Ray Client sends
with every call. The Hub says whose it is, and the call goes on, unread, to
that user's RayCluster: created from raycluster.yaml when they first connect,
running as them, and deleted once idle. Only the gateway holds a cluster's own
Ray token. A cluster runs the global Pixi environment, or the one a notebook
names in the af-ray-env metadata, with one GPU or as many as af-ray-gpus asks.
"""

from __future__ import annotations

import asyncio
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
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import yaml
from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector

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
# How long the clients of a removed cluster hear so, instead of starting another: Ray Client retries for 30 s.
GONE_S = 300.0
LDAP_HOST = "geddes-auth.rcac.purdue.edu"
LDAP_BASE = "ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"
KUBE_API = "https://kubernetes.default.svc"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
RAYCLUSTERS = f"/apis/ray.io/v1/namespaces/{NAMESPACE}/rayclusters"
SECRETS = f"/api/v1/namespaces/{NAMESPACE}/secrets"
MANAGED_BY = "ray-train-gateway"
# Ray Client's port, on the gateway as on every head.
CLIENT_PORT = 10001
# Set by a notebook: ray.init(..., _metadata=[(ENV_METADATA, <environment>)]).
ENV_METADATA = "af-ray-env"
ENV_ANNOTATION = "purdue-af/ray-env"
# The environment pixi-global-sync keeps (apps/af-utils/pixi-global-sync).
DEFAULT_ENV = "/work/pixi/global/.pixi/envs/default"
# Set as af-ray-env is: the cluster's GPUs, one per worker.
GPUS_METADATA = "af-ray-gpus"
GPUS_ANNOTATION = "purdue-af/ray-gpus"
MAX_GPUS = 4
# singleuser.podNameTemplate in the Hub values
SESSION_POD = re.compile(r"purdue-af-(\d+)")
# custom-spawner.py names the accounts from outside Purdue <login>-cern and <login>-fnal
EXTERNAL_SUFFIXES = ("-cern", "-fnal")
# KubeRay's longest RayCluster name: the Services it names after one must fit in 63 characters.
MAX_CLUSTER_NAME = 53
DNS_LABEL = re.compile(r"[a-z]([-a-z0-9]*[a-z0-9])?")
# GRPC_OPTIONS in ray/util/client/common.py: Ray Client's message sizes and keepalives.
GRPC_OPTIONS = [
    ("grpc.max_send_message_length", 2**31 - 1),
    ("grpc.max_receive_message_length", 2**31 - 1),
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_timeout_ms", 600_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
    ("grpc.http2.min_ping_interval_without_data_ms", 29_950),
    ("grpc.http2.max_ping_strikes", 0),
]
# Metadata a relayed call does not carry on: the gateway's own, and what gRPC sets itself.
NOT_RELAYED = {"authorization", ENV_METADATA, "user-agent"}


class Refused(Exception):
    """A call the gateway answers itself."""

    def __init__(self, code: grpc.StatusCode, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


@dataclass(frozen=True)
class User:
    name: str
    account: str
    uid: int
    gid: int

    @property
    def cluster(self) -> str:
        return cluster_name(self.name)


@dataclass(frozen=True)
class Shape:
    """What a user's cluster runs: an environment, and its number of GPUs."""

    env: str
    gpus: str


def cluster_name(username: str) -> str:
    """ray-train-<username>, or for a username that cannot be part of a
    Kubernetes name, what can stay of it and a hash that keeps it apart."""
    name = f"ray-train-{username}"
    if len(name) <= MAX_CLUSTER_NAME and DNS_LABEL.fullmatch(name):
        return name
    stem = re.sub(r"[^a-z0-9]+", "-", username.lower()).strip("-")
    suffix = "---" + hashlib.sha256(username.encode()).hexdigest()[:8]
    return f"ray-train-{stem}"[: MAX_CLUSTER_NAME - len(suffix)] + suffix


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


def pooled_account(af_id: int) -> str:
    """The account a session from outside Purdue runs as (set-user-info.py)."""
    if af_id > 399:
        raise Refused(
            grpc.StatusCode.PERMISSION_DENIED,
            f"There is no pooled account for AF user {af_id}.",
        )
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
        raise Refused(
            grpc.StatusCode.PERMISSION_DENIED, f"There is no LDAP account {account}."
        )
    found = entries[0]["attributes"]
    return int(found["uidNumber"]), int(found["gidNumber"])


def cluster_token(key: str, cluster: str) -> str:
    """Derived rather than stored, so the gateway never reads a Secret."""
    return hmac.new(key.encode(), cluster.encode(), hashlib.sha256).hexdigest()


def head_url(cluster: str) -> str:
    return f"http://{cluster}-head-svc.{NAMESPACE}.svc.cluster.local:8265"


def client_address(cluster: str) -> str:
    return f"{cluster}-head-svc.{NAMESPACE}.svc.cluster.local:{CLIENT_PORT}"


def build_cluster(template: dict[str, Any], user: User, shape: Shape) -> dict[str, Any]:
    cluster = copy.deepcopy(template)
    metadata = cluster["metadata"]
    metadata["name"] = user.cluster
    metadata.setdefault("labels", {})["app.kubernetes.io/managed-by"] = MANAGED_BY
    metadata.setdefault("annotations", {}).update(
        {ENV_ANNOTATION: shape.env, GPUS_ANNOTATION: shape.gpus}
    )
    spec = cluster["spec"]
    spec["authOptions"]["secretName"] = user.cluster
    (group,) = spec["workerGroupSpecs"]
    workers = int(shape.gpus)
    group.update(replicas=workers, minReplicas=workers, maxReplicas=workers)
    for template in (spec["headGroupSpec"]["template"], group["template"]):
        pod = template["spec"]
        pod.setdefault("securityContext", {}).update(
            {"runAsUser": user.uid, "runAsGroup": user.gid}
        )
        for container in pod["containers"]:
            variables = container.setdefault("env", [])
            # `ray start`, and with it every Ray process, then come from the environment.
            for variable in variables:
                if variable["name"] == "PATH":
                    variable["value"] = f"{shape.env}/bin:{variable['value']}"
            variables.append({"name": "CONDA_PREFIX", "value": shape.env})
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


def head_ready(cluster: dict[str, Any]) -> bool:
    conditions = (cluster.get("status") or {}).get("conditions") or []
    return any(
        c.get("type") == "HeadPodReady" and c.get("status") == "True"
        for c in conditions
    )


def load_template() -> dict[str, Any]:
    template: dict[str, Any] = yaml.safe_load(TEMPLATE.read_text())
    return template


def env_roots(template: dict[str, Any]) -> list[str]:
    """Where an environment may live: the storage a cluster mounts."""
    pod = template["spec"]["headGroupSpec"]["template"]["spec"]
    scratch = {v["name"] for v in pod["volumes"] if "emptyDir" in v}
    (container,) = pod["containers"]
    return [
        m["mountPath"] for m in container["volumeMounts"] if m["name"] not in scratch
    ]


def requested_env(value: str, template: dict[str, Any]) -> str:
    """The environment a call asks for, or the global one."""
    if not value:
        return DEFAULT_ENV
    path = posixpath.normpath(value)
    roots = env_roots(template)
    if not value.startswith("/") or not any(
        path == root or path.startswith(root.rstrip("/") + "/") for root in roots
    ):
        raise Refused(
            grpc.StatusCode.INVALID_ARGUMENT,
            f"{value} is not on storage your Ray cluster mounts: {', '.join(roots)}.",
        )
    return path


def requested_gpus(value: str) -> str:
    """The GPUs a call asks for, one by default."""
    if not value:
        return "1"
    if value in {str(n) for n in range(1, MAX_GPUS + 1)}:
        return value
    raise Refused(
        grpc.StatusCode.INVALID_ARGUMENT,
        f"{GPUS_METADATA} is a number of GPUs from 1 to {MAX_GPUS}.",
    )


def cluster_shape(cluster: dict[str, Any]) -> Shape:
    annotations = cluster["metadata"].get("annotations") or {}
    return Shape(
        str(annotations.get(ENV_ANNOTATION, "")),
        str(annotations.get(GPUS_ANNOTATION, "")),
    )


def runs(cluster: dict[str, Any], shape: Shape) -> bool:
    """Whether the cluster stays up and is of `shape`."""
    return (
        not cluster["metadata"].get("deletionTimestamp")
        and cluster_shape(cluster) == shape
    )


class Gateway:
    def __init__(
        self, http: ClientSession, kube: ClientSession, token_file: Path
    ) -> None:
        self.http = http
        self.kube_http = kube
        self.token_file = token_file
        self.last_used: dict[str, float] = {}
        # The shape each cluster was last found in, so that a call skips the checks.
        self.started: dict[str, Shape] = {}
        self.channels: dict[str, grpc.aio.Channel] = {}
        # cluster -> the Ray Client ids it serves; client id -> until when its cluster is known removed
        self.clients: dict[str, set[str]] = {}
        self.gone: dict[str, float] = {}
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

    async def relay(
        self,
        method: str,
        requests: AsyncIterator[bytes],
        context: grpc.aio.ServicerContext[bytes, bytes],
    ) -> AsyncIterator[bytes]:
        metadata = tuple(context.invocation_metadata() or ())
        try:
            cluster = await self.cluster_for(metadata)
        except Refused as e:
            await context.abort(e.code, e.text)
        forwarded = [(k, v) for k, v in metadata if k not in NOT_RELAYED]
        forwarded.append(("authorization", self.cluster_auth(cluster)["Authorization"]))
        relayed: grpc.aio.StreamStreamMultiCallable[bytes, bytes] = self.channel(
            cluster
        ).stream_stream(method)
        call = relayed(self.tracked(cluster, requests), metadata=forwarded)
        try:
            async for response in call:
                yield response
        except grpc.aio.AioRpcError as e:
            if e.code() == grpc.StatusCode.UNAVAILABLE:
                self.started.pop(cluster, None)
            await context.abort(e.code(), e.details() or "")

    async def tracked(
        self, cluster: str, requests: AsyncIterator[bytes]
    ) -> AsyncIterator[bytes]:
        """What the notebook sends keeps its cluster; what the cluster sends, its logs among it, does not."""
        async for request in requests:
            self.last_used[cluster] = time.monotonic()
            yield request

    async def cluster_for(self, metadata: Sequence[tuple[str, str | bytes]]) -> str:
        """The caller's cluster, started for the environment the call asks for."""
        if not self.service_token:
            raise Refused(
                grpc.StatusCode.UNAVAILABLE,
                "Ray is not enabled: JupyterHub has not registered the ray-train-gateway service.",
            )
        values = {key: str(value) for key, value in metadata}
        token = bearer_token(values.get("authorization", ""))
        if token is None:
            raise Refused(
                grpc.StatusCode.UNAUTHENTICATED,
                "No token: RAY_AUTH_MODE=token and RAY_AUTH_TOKEN=$JUPYTERHUB_API_TOKEN must be set.",
            )
        user = await self.user_for(token)
        shape = Shape(
            requested_env(values.get(ENV_METADATA, ""), load_template()),
            requested_gpus(values.get(GPUS_METADATA, "")),
        )
        now = time.monotonic()
        self.gone = {c: until for c, until in self.gone.items() if until > now}
        client = values.get("client_id", "")
        # Ray Client reconnecting: its session went with the cluster.
        if client in self.gone:
            raise Refused(
                grpc.StatusCode.NOT_FOUND,
                "Your Ray cluster was removed after it went idle: run ray.shutdown(), then ray.init() again.",
            )
        self.last_used[user.cluster] = now
        if self.started.get(user.cluster) != shape:
            await self.start_cluster(user, shape)
            self.started[user.cluster] = shape
        self.clients.setdefault(user.cluster, set()).add(client)
        self.gone.pop(client, None)
        return user.cluster

    def channel(self, cluster: str) -> grpc.aio.Channel:
        if cluster not in self.channels:
            self.channels[cluster] = grpc.aio.insecure_channel(
                client_address(cluster), options=GRPC_OPTIONS
            )
        return self.channels[cluster]

    async def user_for(self, token: str) -> User:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        cached = self._users.get(key)
        if cached and cached[0] > now:
            return cached[1]
        name = await self.whoami(token)
        account = name
        if name.endswith(EXTERNAL_SUFFIXES):
            # Numbered after the hub id, which the pod name of the user's session carries.
            af_id = session_af_id(await self.hub_user(name))
            if af_id is None:
                raise Refused(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "Start your AF session first: a Ray cluster runs as the user of a running session.",
                )
            account = pooled_account(af_id)
        uid, gid = await asyncio.to_thread(ldap_ids, account)
        user = User(name=name, account=account, uid=uid, gid=gid)
        self._users = {k: v for k, v in self._users.items() if v[0] > now}
        self._users[key] = (now + USER_CACHE_S, user)
        return user

    async def whoami(self, token: str) -> str:
        async with self.http.get(
            f"{HUB_API}/user", headers={"Authorization": f"token {token}"}
        ) as r:
            if r.status in (401, 403):
                raise Refused(
                    grpc.StatusCode.UNAUTHENTICATED,
                    "JupyterHub did not accept RAY_AUTH_TOKEN: it must be this session's JUPYTERHUB_API_TOKEN.",
                )
            if r.status != 200:
                raise Refused(
                    grpc.StatusCode.UNAVAILABLE, f"JupyterHub answered HTTP {r.status}."
                )
            model: dict[str, Any] = await r.json()
        return str(model["name"])

    async def hub_user(self, name: str) -> dict[str, Any]:
        async with self.http.get(
            f"{HUB_API}/users/{urllib.parse.quote(name, safe='')}",
            headers={"Authorization": f"token {self.service_token}"},
        ) as r:
            if r.status != 200:
                raise Refused(
                    grpc.StatusCode.UNAVAILABLE,
                    f"JupyterHub answered HTTP {r.status} for the user's session.",
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
            raise Refused(
                grpc.StatusCode.UNAVAILABLE,
                f"Kubernetes answered HTTP {status} for the Ray cluster.",
            )
        return body

    async def start_cluster(self, user: User, shape: Shape) -> None:
        """Create the user's cluster in `shape` unless it is in it; return once its head answers."""
        cluster = await self.cluster(user.cluster)
        if cluster is not None and not runs(cluster, shape):
            if not cluster["metadata"].get("deletionTimestamp"):
                if await self.busy(cluster):
                    raise Refused(
                        grpc.StatusCode.FAILED_PRECONDITION,
                        "Your Ray cluster runs another environment or number of GPUs and is running tasks: wait for them, or stop them.",
                    )
                await self.delete(user.cluster)
            await self.wait_until_gone(user.cluster)
            cluster = None
        if cluster is None:
            status, created = await self.kube(
                "POST", RAYCLUSTERS, build_cluster(load_template(), user, shape)
            )
            if status == 201:
                log.info("created %s", user.cluster)
                cluster = created
            elif status == 409:
                # Another call created it first.
                cluster = await self.cluster(user.cluster)
            else:
                raise Refused(
                    grpc.StatusCode.UNAVAILABLE,
                    f"Could not create your Ray cluster: {created.get('message', status)}",
                )
        if cluster is None or not runs(cluster, shape):
            raise Refused(
                grpc.StatusCode.ABORTED,
                "Another call is replacing your Ray cluster; connect again in a minute.",
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
                raise Refused(
                    grpc.StatusCode.UNAVAILABLE,
                    f"Could not create your Ray cluster's token: {body.get('message', status)}",
                )
            if await self.head_status(user.cluster) == 200:
                break
            if time.monotonic() > deadline:
                raise Refused(
                    grpc.StatusCode.UNAVAILABLE,
                    "Your Ray cluster did not start in time: no T4 may be free. Try again later.",
                )
            await asyncio.sleep(START_POLL_S)
        # A Ray that ignores RAY_AUTH_MODE would serve anyone who reaches it.
        if await self.head_status(user.cluster, authorized=False) == 200:
            await self.delete(user.cluster)
            raise Refused(
                grpc.StatusCode.INVALID_ARGUMENT,
                "The Ray in that environment does not enforce the cluster's token.",
            )

    async def delete(self, name: str) -> None:
        # Foreground: the RayCluster outlasts its pod, so waiting it out frees the user's one GPU.
        await self.kube(
            "DELETE", f"{RAYCLUSTERS}/{name}", {"propagationPolicy": "Foreground"}
        )
        self.started.pop(name, None)
        until = time.monotonic() + GONE_S
        for client in self.clients.pop(name, set()):
            self.gone[client] = until
        channel = self.channels.pop(name, None)
        if channel is not None:
            await channel.close()

    async def wait_until_gone(self, name: str) -> None:
        deadline = time.monotonic() + START_TIMEOUT_S
        while await self.cluster(name) is not None:
            if time.monotonic() > deadline:
                raise Refused(
                    grpc.StatusCode.UNAVAILABLE,
                    "Your previous Ray cluster is still shutting down; connect again in a minute.",
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
        """A running task keeps a cluster; so does a ready head that did not answer."""
        name = cluster["metadata"]["name"]
        try:
            async with self.http.get(
                head_url(name) + "/api/v0/tasks",
                params={
                    "filter_keys": "state",
                    "filter_predicates": "=",
                    "filter_values": "RUNNING",
                    "limit": "1",
                },
                headers=self.cluster_auth(name),
                timeout=ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    listing: dict[str, Any] = await r.json()
                    return bool(listing["data"]["result"]["result"])
                if r.status in (401, 403):
                    # Made under another service token: nothing can reach it any more.
                    return False
        except (ClientError, asyncio.TimeoutError):
            pass
        return head_ready(cluster)

    async def close(self) -> None:
        for channel in self.channels.values():
            await channel.close()


class Relay(grpc.GenericRpcHandler):
    """Every call, whatever its method, relayed as a stream of raw messages."""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    def service(
        self, handler_call_details: grpc.HandlerCallDetails
    ) -> grpc.RpcMethodHandler[bytes, bytes]:
        method = handler_call_details.method

        async def relay(
            requests: AsyncIterator[bytes],
            context: grpc.aio.ServicerContext[bytes, bytes],
        ) -> AsyncIterator[bytes]:
            async for response in self.gateway.relay(method, requests, context):
                yield response

        return grpc.stream_stream_rpc_method_handler(relay)


async def reap_forever(gateway: Gateway) -> None:
    while True:
        await asyncio.sleep(REAP_EVERY_S)
        try:
            await gateway.reap_idle()
        except Exception:
            log.exception("reaping idle Ray clusters")


async def serve() -> None:
    kube_tls = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    async with (
        ClientSession() as http,
        ClientSession(connector=TCPConnector(ssl=kube_tls)) as kube,
    ):
        gateway = Gateway(http, kube, SERVICE_TOKEN_FILE)
        server = grpc.aio.server(options=GRPC_OPTIONS)
        server.add_generic_rpc_handlers((Relay(gateway),))
        server.add_insecure_port(f"0.0.0.0:{CLIENT_PORT}")
        await server.start()
        try:
            await reap_forever(gateway)
        finally:
            await server.stop(None)
            await gateway.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(serve())
