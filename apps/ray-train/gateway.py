"""Ray gateway: each AF user's Ray Client, `ray.init("ray://ray-train-gateway:10001")`,
and Jobs API client, `JobSubmissionClient("http://ray-train-gateway:8265")`,
reach a Ray cluster of their own.

A session's RAY_AUTH_TOKEN is its JupyterHub token, which both clients send
with every call. The Hub says whose it is, and the call goes on, unread, to
that user's RayCluster: created from raycluster.yaml when they first connect
or submit a job, running as them, and deleted once idle. Only the gateway
holds a cluster's own Ray token. A cluster runs the global Pixi environment,
or the one a call names in `env`, with one GPU or as many as `gpus`
asks, each of the memory `min-memory-per-gpu` asks or more: a cluster starts only
while that many such GPUs are free, or with one GPU while another cluster's
preemptible worker holds one. The settings are in config.yaml.
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
import signal
import ssl
import time
import urllib.parse
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import grpc
import yaml
from aiohttp import (
    ClientError,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    TCPConnector,
    WSMsgType,
    WSServerHandshakeError,
    web,
)
from gpu_queries import ALLOC_QUERY, GPU_METRICS, PREEMPTIBLE_QUERY, USED_QUERY
from yarl import URL

log = logging.getLogger("ray-train-gateway")

NAMESPACE = os.environ.get("NAMESPACE", "cms")
HUB_API = os.environ.get("JUPYTERHUB_API_URL", "http://hub:8081/hub/api")
TEMPLATE = Path("/app/raycluster.yaml")
CODE = Path(__file__)
# Beside this file in the ConfigMap, as in the repository.
CONFIG_FILE = CODE.with_name("config.yaml")
CONFIG: dict[str, Any] = yaml.safe_load(CONFIG_FILE.read_text())
CODE_POLL_S = float(CONFIG["codePollSeconds"])
IDLE_TIMEOUT_S = float(CONFIG["idleTimeoutSeconds"])
START_TIMEOUT_S = float(CONFIG["startTimeoutSeconds"])
START_POLL_S = float(CONFIG["startPollSeconds"])
PROVISION_TIMEOUT_S = float(CONFIG["provisionTimeoutSeconds"])
REAP_EVERY_S = float(CONFIG["reapEverySeconds"])
USER_CACHE_S = float(CONFIG["userCacheSeconds"])
GONE_S = float(CONFIG["goneSeconds"])
GRANT_S = float(CONFIG["grantSeconds"])


@dataclass(frozen=True)
class Flavor:
    """A kind of GPU a worker may hold."""

    resource: str
    memory: float
    budget: int

    @property
    def group(self) -> str:
        """The name of its worker group: `gpu` for nvidia.com/gpu."""
        return re.sub(r"[^a-z0-9]+", "-", self.resource.rpartition("/")[2])


FLAVORS = [
    Flavor(str(g["resource"]), float(g["memoryGb"]), int(g["budget"]))
    for g in CONFIG["gpus"]
]
PREEMPTIBLE_PRIORITY_CLASS = str(CONFIG["preemptiblePriorityClass"])
PROMETHEUS_URL = str(CONFIG["prometheusUrl"])
DEFAULT_ENV = str(CONFIG["defaultEnv"])
LDAP_HOST = str(CONFIG["ldapHost"])
LDAP_BASE = str(CONFIG["ldapBase"])
# This service's own Hub token, from the `hub` Secret: the file appears once the Hub registers the service.
SERVICE_TOKEN_FILE = Path("/etc/hub-token/token")
KUBE_API = "https://kubernetes.default.svc"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
RAYCLUSTERS = f"/apis/ray.io/v1/namespaces/{NAMESPACE}/rayclusters"
SECRETS = f"/api/v1/namespaces/{NAMESPACE}/secrets"
MANAGED_BY = "ray-train-gateway"
# Ray Client's port, on the gateway as on every head.
CLIENT_PORT = 10001
# The dashboard's, whose HTTP API, the Jobs API's among it, the gateway relays.
DASHBOARD_PORT = 8265
# Set by a notebook: ray.init(..., _metadata=[(ENV_METADATA, <environment>)]), or a header of JobSubmissionClient.
ENV_METADATA = "env"
ENV_ANNOTATION = "purdue-af/ray-env"
# Set as `env` is: the cluster's GPUs, one per worker.
GPUS_METADATA = "gpus"
GPUS_ANNOTATION = "purdue-af/ray-gpus"
# Set as `env` is: the least memory a worker's GPU may have, in GB.
MEMORY_METADATA = "min-memory-per-gpu"
MEMORY_ANNOTATION = "purdue-af/ray-gpu-memory"
REMOVED_IDLE = "Your Ray cluster was removed after it went idle: run ray.shutdown(), then ray.init() again."
REMOVED_UNPROVISIONED = f"Your Ray cluster was removed, as not all of its GPUs started in time: ask for fewer with {GPUS_METADATA}, or try again later."
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
NOT_RELAYED = {
    "authorization",
    ENV_METADATA,
    GPUS_METADATA,
    MEMORY_METADATA,
    "user-agent",
}
# Headers a relayed HTTP call carries on neither way: the gateway's own, and its connections' framing.
NOT_PASSED_ON = {
    "authorization",
    "host",
    ENV_METADATA,
    GPUS_METADATA,
    MEMORY_METADATA,
    "connection",
    "keep-alive",
    "upgrade",
    "transfer-encoding",
    "content-length",
}
# A long log tail or upload is no failure; only a head that cannot be reached is.
RELAY_TIMEOUT = ClientTimeout(total=None, sock_connect=10)
# How the Jobs API's client shows a refusal: 401 and 403 as authentication errors, the rest with their text.
HTTP_STATUS = {
    grpc.StatusCode.INVALID_ARGUMENT: 400,
    grpc.StatusCode.UNAUTHENTICATED: 401,
    grpc.StatusCode.PERMISSION_DENIED: 403,
    grpc.StatusCode.NOT_FOUND: 404,
    grpc.StatusCode.FAILED_PRECONDITION: 409,
    grpc.StatusCode.ABORTED: 409,
    grpc.StatusCode.RESOURCE_EXHAUSTED: 429,
    grpc.StatusCode.UNAVAILABLE: 503,
}


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
    """What a user's cluster runs: an environment, its number of GPUs, and the
    memory of the smallest GPU it may hold, in GB."""

    env: str
    gpus: str
    memory: str

    @property
    def flavors(self) -> list[Flavor]:
        return [f for f in FLAVORS if f.memory >= float(self.memory)]


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
    return f"http://{cluster}-head-svc.{NAMESPACE}.svc.cluster.local:{DASHBOARD_PORT}"


def client_address(cluster: str) -> str:
    return f"{cluster}-head-svc.{NAMESPACE}.svc.cluster.local:{CLIENT_PORT}"


def passed_on(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """What a relayed HTTP call or answer carries on of its headers."""
    return [
        (key, value)
        for key, value in headers.items()
        if key.lower() not in NOT_PASSED_ON
        and not key.lower().startswith("sec-websocket-")
    ]


def submits(request: web.Request) -> bool:
    """Whether an HTTP call submits a job, or checks for or uploads a package it takes along."""
    return request.path.startswith("/api/packages/") or (
        request.method == "POST" and request.path == "/api/jobs/"
    )


def build_cluster(
    template: dict[str, Any], user: User, shape: Shape, mix: dict[str, int]
) -> dict[str, Any]:
    """The user's cluster, with `mix` workers of each GPU resource."""
    cluster = copy.deepcopy(template)
    metadata = cluster["metadata"]
    metadata["name"] = user.cluster
    metadata.setdefault("labels", {})["app.kubernetes.io/managed-by"] = MANAGED_BY
    metadata.setdefault("annotations", {}).update(
        {
            ENV_ANNOTATION: shape.env,
            GPUS_ANNOTATION: shape.gpus,
            MEMORY_ANNOTATION: shape.memory,
        }
    )
    spec = cluster["spec"]
    spec["authOptions"]["secretName"] = user.cluster
    (group,) = spec["workerGroupSpecs"]
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
    spec["workerGroupSpecs"] = []
    # Every worker but the first gives way to pods of the default priority.
    kept = 1
    for flavor in FLAVORS:
        workers = mix.get(flavor.resource, 0)
        for replicas, preemptible in (
            (min(kept, workers), False),
            (workers - kept, True),
        ):
            if replicas > 0:
                spec["workerGroupSpecs"].append(
                    worker_group(group, flavor, replicas, preemptible)
                )
        kept = max(kept - workers, 0)
    return cluster


def worker_group(
    template: dict[str, Any], flavor: Flavor, replicas: int, preemptible: bool
) -> dict[str, Any]:
    """The template's worker group, as `replicas` workers with a GPU of `flavor` each."""
    group = copy.deepcopy(template)
    group["groupName"] = flavor.group + ("-preemptible" if preemptible else "")
    group.update(replicas=replicas, minReplicas=replicas, maxReplicas=replicas)
    pod = group["template"]["spec"]
    if preemptible:
        pod["priorityClassName"] = PREEMPTIBLE_PRIORITY_CLASS
    for container in pod["containers"]:
        for amounts in container["resources"].values():
            for other in FLAVORS:
                amounts.pop(other.resource, None)
            amounts[flavor.resource] = 1
    return group


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


def unprovisioned(cluster: dict[str, Any], now: float) -> bool:
    """Whether the cluster still waits for its first full set of pods, past the time it has for it."""
    metadata = cluster["metadata"]
    conditions = (cluster.get("status") or {}).get("conditions") or []
    if metadata.get("deletionTimestamp") or not any(
        c.get("type") == "RayClusterProvisioned" and c.get("status") == "False"
        for c in conditions
    ):
        return False
    created = datetime.fromisoformat(metadata["creationTimestamp"])
    return now - created.timestamp() >= PROVISION_TIMEOUT_S


def held_gpus(cluster: dict[str, Any]) -> dict[str, int]:
    """The workers the cluster has of each GPU resource."""
    held: dict[str, int] = {}
    for group in cluster["spec"].get("workerGroupSpecs") or []:
        for container in group["template"]["spec"]["containers"]:
            limits = (container.get("resources") or {}).get("limits") or {}
            for flavor in FLAVORS:
                if flavor.resource in limits:
                    held[flavor.resource] = held.get(flavor.resource, 0) + int(
                        group.get("replicas", 0)
                    )
    return held


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


def requested_memory(value: str) -> str:
    """The GPU memory a call asks of each worker, as that of the smallest GPU that has it."""
    try:
        wanted = float(value or 0)
    except ValueError:
        raise Refused(
            grpc.StatusCode.INVALID_ARGUMENT,
            f"{MEMORY_METADATA} is a number of GB.",
        ) from None
    enough = [f.memory for f in FLAVORS if f.memory >= wanted]
    if not enough:
        raise Refused(
            grpc.StatusCode.INVALID_ARGUMENT,
            f"No GPU of a Ray cluster has {value} GB: the largest has {max(f.memory for f in FLAVORS):g} GB.",
        )
    return f"{min(enough):g}"


def requested_gpus(value: str, flavors: list[Flavor]) -> str:
    """The GPUs a call asks for, one by default."""
    if not value:
        return "1"
    most = sum(f.budget for f in flavors)
    if value in {str(n) for n in range(1, most + 1)}:
        return value
    raise Refused(
        grpc.StatusCode.INVALID_ARGUMENT,
        f"{GPUS_METADATA} is a number of GPUs from 1 to {most}.",
    )


def cluster_shape(cluster: dict[str, Any]) -> Shape:
    annotations = cluster["metadata"].get("annotations") or {}
    return Shape(
        str(annotations.get(ENV_ANNOTATION, "")),
        str(annotations.get(GPUS_ANNOTATION, "")),
        # Without it, a cluster holds GPUs that any call may have.
        str(annotations.get(MEMORY_ANNOTATION, requested_memory(""))),
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
        # cluster -> the Ray Client ids it serves; client id -> until when its cluster is known removed, and why
        self.clients: dict[str, set[str]] = {}
        self.gone: dict[str, tuple[float, str]] = {}
        # cluster -> why it was removed before it ever ran, until the user's next one
        self.never_started: dict[str, str] = {}
        # cluster -> the GPUs it was admitted with, and until when they count as taken
        self.grants: dict[str, tuple[dict[str, int], float]] = {}
        self.admitting = asyncio.Lock()
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
        values = {key: str(value) for key, value in metadata}
        client = values.get("client_id", "")
        try:
            user, shape = await self.caller(values)
            now = time.monotonic()
            self.gone = {c: gone for c, gone in self.gone.items() if gone[0] > now}
            # Ray Client reconnecting: its session went with the cluster.
            if client in self.gone:
                raise Refused(grpc.StatusCode.NOT_FOUND, self.gone[client][1])
            cluster = await self.cluster_for(user, shape)
        except Refused as e:
            await context.abort(e.code, e.text)
        self.clients.setdefault(cluster, set()).add(client)
        self.gone.pop(client, None)
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

    async def caller(self, values: dict[str, str]) -> tuple[User, Shape]:
        """Who makes a call, from its token, and the cluster it asks for."""
        if not self.service_token:
            raise Refused(
                grpc.StatusCode.UNAVAILABLE,
                "Ray is not enabled: JupyterHub has not registered the ray-train-gateway service.",
            )
        token = bearer_token(values.get("authorization", ""))
        if token is None:
            raise Refused(
                grpc.StatusCode.UNAUTHENTICATED,
                "No token: RAY_AUTH_MODE=token and RAY_AUTH_TOKEN=$JUPYTERHUB_API_TOKEN must be set.",
            )
        user = await self.user_for(token)
        memory = requested_memory(values.get(MEMORY_METADATA, ""))
        shape = Shape(
            requested_env(values.get(ENV_METADATA, ""), load_template()),
            "1",
            memory,
        )
        gpus = requested_gpus(values.get(GPUS_METADATA, ""), shape.flavors)
        return user, Shape(shape.env, gpus, memory)

    async def cluster_for(self, user: User, shape: Shape) -> str:
        """The user's cluster, started in `shape` unless it runs in it."""
        self.last_used[user.cluster] = time.monotonic()
        if self.started.get(user.cluster) != shape:
            await self.start_cluster(user, shape)
            self.started[user.cluster] = shape
        return user.cluster

    async def running(self, user: User) -> str:
        """The user's cluster in whatever shape: asking about jobs starts none."""
        if user.cluster not in self.started:
            cluster = await self.cluster(user.cluster)
            if cluster is None or cluster["metadata"].get("deletionTimestamp"):
                raise Refused(
                    grpc.StatusCode.NOT_FOUND,
                    self.never_started.get(
                        user.cluster,
                        "You have no Ray cluster: one is removed once idle, with the records and logs of its jobs.",
                    ),
                )
        self.last_used[user.cluster] = time.monotonic()
        return user.cluster

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """An HTTP call to a dashboard, the Jobs API's among them, relayed to the caller's cluster."""
        try:
            user, shape = await self.caller(
                {key.lower(): value for key, value in request.headers.items()}
            )
            if request.path == "/api/version":
                # Answered here, so that a client asking after its jobs starts no cluster: the oldest Ray a cluster runs passes every check the client makes.
                return web.json_response(
                    {"ray_version": load_template()["spec"]["rayVersion"]}
                )
            if submits(request):
                cluster = await self.cluster_for(user, shape)
            else:
                cluster = await self.running(user)
            url = URL(head_url(cluster) + request.rel_url.raw_path_qs, encoded=True)
            headers = passed_on(request.headers)
            headers += self.cluster_auth(cluster).items()
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return await self.relay_logs(request, cluster, url, headers)
            return await self.relay_http(request, cluster, url, headers)
        except Refused as e:
            # JobSubmissionClient ends the text with a period of its own.
            text = e.text.removesuffix(".")
            return web.Response(status=HTTP_STATUS[e.code], reason=text, text=text)

    async def relay_http(
        self,
        request: web.Request,
        cluster: str,
        url: URL,
        headers: list[tuple[str, str]],
    ) -> web.StreamResponse:
        """The call's body and the head's answer, passed on as they come."""
        try:
            upstream = await self.http.request(
                request.method,
                url,
                headers=headers,
                data=request.content if request.body_exists else None,
                allow_redirects=False,
                auto_decompress=False,
                timeout=RELAY_TIMEOUT,
            )
        except (ClientError, asyncio.TimeoutError) as e:
            self.started.pop(cluster, None)
            raise Refused(
                grpc.StatusCode.UNAVAILABLE,
                "Your Ray cluster did not answer: try again in a minute.",
            ) from e
        async with upstream:
            response = web.StreamResponse(
                status=upstream.status,
                reason=upstream.reason,
                headers=passed_on(upstream.headers),
            )
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
        return response

    async def relay_logs(
        self,
        request: web.Request,
        cluster: str,
        url: URL,
        headers: list[tuple[str, str]],
    ) -> web.StreamResponse:
        """A job's log tail: a WebSocket the head writes lines to until the job ends."""
        try:
            upstream = await self.http.ws_connect(url, headers=headers)
        except WSServerHandshakeError as e:
            return web.Response(status=e.status, text=e.message)
        except (ClientError, asyncio.TimeoutError) as e:
            self.started.pop(cluster, None)
            raise Refused(
                grpc.StatusCode.UNAVAILABLE,
                "Your Ray cluster did not answer: try again in a minute.",
            ) from e
        async with upstream:
            response = web.WebSocketResponse()
            await response.prepare(request)
            # A session that left is noticed only by the next line sent to it.
            with contextlib.suppress(ConnectionResetError):
                async for message in upstream:
                    if message.type != WSMsgType.TEXT:
                        break
                    await response.send_str(message.data)
                await response.close()
        return response

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
                        "Your Ray cluster runs another environment, number of GPUs or GPU memory, and tasks or jobs: wait for them, or stop them.",
                    )
                # Before the deletion: a refusal leaves the user the cluster they have.
                mix = await self.admit(user, shape, held_gpus(cluster))
                await self.delete(user.cluster)
            else:
                mix = await self.admit(user, shape)
            await self.wait_until_gone(user.cluster)
            cluster = None
        elif cluster is None:
            mix = await self.admit(user, shape)
        if cluster is None:
            status, created = await self.kube(
                "POST", RAYCLUSTERS, build_cluster(load_template(), user, shape, mix)
            )
            if status == 201:
                log.info("created %s", user.cluster)
                self.never_started.pop(user.cluster, None)
                cluster = created
            elif status == 409:
                # Another call created it first.
                cluster = await self.cluster(user.cluster)
            else:
                self.grants.pop(user.cluster, None)
                raise Refused(
                    grpc.StatusCode.UNAVAILABLE,
                    f"Could not create your Ray cluster: {created.get('message', status)}",
                )
        if cluster is None or not runs(cluster, shape):
            raise Refused(
                grpc.StatusCode.ABORTED,
                "Another call is replacing your Ray cluster: try again in a minute.",
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
                    "Your Ray cluster's head did not start in time: try again later.",
                )
            await asyncio.sleep(START_POLL_S)
        # A Ray that ignores RAY_AUTH_MODE would serve anyone who reaches it.
        if await self.head_status(user.cluster, authorized=False) == 200:
            await self.delete(user.cluster)
            raise Refused(
                grpc.StatusCode.INVALID_ARGUMENT,
                "The Ray in that environment does not enforce the cluster's token.",
            )

    async def admit(
        self, user: User, shape: Shape, replaced: dict[str, int] | None = None
    ) -> dict[str, int]:
        """The workers a cluster gets of each GPU resource its shape allows, the
        first of FLAVORS first. Refuse one whose GPUs are not all free now, unless
        it asks for one and a preemptible worker holds one; `replaced` are the GPUs
        its predecessor gives back."""
        wanted = int(shape.gpus)
        replaced = replaced or {}
        async with self.admitting:
            status, listing = await self.kube(
                "GET",
                f"{RAYCLUSTERS}?labelSelector=app.kubernetes.io%2Fmanaged-by%3D{MANAGED_BY}",
            )
            if status != 200:
                raise Refused(
                    grpc.StatusCode.UNAVAILABLE,
                    f"Kubernetes answered HTTP {status} for the Ray clusters.",
                )
            now = time.monotonic()
            self.grants = {c: g for c, g in self.grants.items() if g[1] > now}
            others = {
                c["metadata"]["name"]: held_gpus(c)
                for c in listing.get("items", [])
                if c["metadata"]["name"] != user.cluster
            }
            granted = {
                c: gpus for c, (gpus, _) in self.grants.items() if c != user.cluster
            }
            free = await self.free_gpus()
            mix: dict[str, int] = {}
            evicting = None
            for flavor in shape.flavors:
                resource = flavor.resource
                taken = sum(g.get(resource, 0) for g in granted.values())
                if free is None:
                    # A cluster admitted and not created yet is in no listing.
                    held = {**granted, **others}.values()
                    available = flavor.budget - sum(h.get(resource, 0) for h in held)
                else:
                    unheld, preemptible = free[resource]
                    # Prometheus may not see the pods of a cluster just admitted.
                    available = unheld + replaced.get(resource, 0) - taken
                    # Its predecessor's preemptible workers leave with it.
                    if unheld + preemptible - replaced.get(resource, 0) - taken > 0:
                        evicting = evicting or resource
                mix[resource] = max(min(available, wanted - sum(mix.values())), 0)
            available = sum(mix.values())
            if wanted == 1 and not available and evicting:
                # Its worker evicts a preemptible one.
                mix[evicting] = available = 1
            if wanted > available:
                log.info(
                    "refused %s %d GPUs: %d available, %s free",
                    user.cluster,
                    wanted,
                    available,
                    free,
                )
                raise Refused(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    f"Your Ray cluster asks for {wanted} GPUs of {shape.memory} GB or more and Ray clusters can take {available} more now: ask for fewer with {GPUS_METADATA}, or try again later.",
                )
            self.grants[user.cluster] = (mix, now + GRANT_S)
            return mix

    async def free_gpus(self) -> dict[str, tuple[int, int]] | None:
        """Of each GPU resource, those no pod holds, and those preemptible pods hold,
        which the Hub's profile form counts as free; None if Prometheus does not say."""
        try:
            allocatable, used, preemptible = await asyncio.gather(
                self.prometheus(ALLOC_QUERY),
                self.prometheus(USED_QUERY),
                self.prometheus(PREEMPTIBLE_QUERY),
            )
            free = {}
            for flavor in FLAVORS:
                metric = GPU_METRICS[flavor.resource]
                if metric not in allocatable:
                    return None
                evictable = int(preemptible.get(metric, 0))
                held = used.get(metric, 0) + evictable
                unheld = max(int(allocatable[metric] - held), 0)
                free[flavor.resource] = (unheld, evictable)
            return free
        except (ClientError, asyncio.TimeoutError, LookupError, TypeError, ValueError):
            log.warning("Prometheus did not say how many GPUs are free")
            return None

    async def prometheus(self, query: str) -> dict[str, float]:
        async with self.http.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=ClientTimeout(total=5),
            raise_for_status=True,
        ) as r:
            samples = (await r.json())["data"]["result"]
        return {s["metric"]["resource"]: float(s["value"][1]) for s in samples}

    async def delete(self, name: str, why: str = REMOVED_IDLE) -> None:
        # Foreground: the RayCluster outlasts its pods, so waiting it out frees the user's GPUs.
        status, _ = await self.kube(
            "DELETE", f"{RAYCLUSTERS}/{name}", {"propagationPolicy": "Foreground"}
        )
        if status not in (200, 202, 404):
            raise Refused(
                grpc.StatusCode.UNAVAILABLE,
                f"Kubernetes answered HTTP {status} deleting the Ray cluster.",
            )
        self.started.pop(name, None)
        until = time.monotonic() + GONE_S
        for client in self.clients.pop(name, set()):
            self.gone[client] = (until, why)
        channel = self.channels.pop(name, None)
        if channel is not None:
            await channel.close()

    async def wait_until_gone(self, name: str) -> None:
        deadline = time.monotonic() + START_TIMEOUT_S
        while await self.cluster(name) is not None:
            if time.monotonic() > deadline:
                raise Refused(
                    grpc.StatusCode.UNAVAILABLE,
                    "Your previous Ray cluster is still shutting down: try again in a minute.",
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
            try:
                # Busy or not: its workers hold GPUs for work that waits on the rest.
                if unprovisioned(cluster, time.time()):
                    await self.delete(name, REMOVED_UNPROVISIONED)
                    self.last_used.pop(name, None)
                    self.never_started[name] = REMOVED_UNPROVISIONED
                    log.info("deleted %s: its workers did not all start", name)
                # Its idle clock starts when its last task or job ends, or its last call does.
                elif await self.busy(cluster):
                    self.last_used[name] = now
                # A cluster first seen here (the gateway restarted) starts its idle clock now.
                elif now - self.last_used.setdefault(name, now) >= IDLE_TIMEOUT_S:
                    await self.delete(name)
                    self.last_used.pop(name, None)
                    log.info("deleted idle %s", name)
            except Exception:
                log.exception("checking %s", name)

    async def busy(self, cluster: dict[str, Any]) -> bool:
        """A running task or job keeps a cluster; so does a ready head that did not
        answer, or answered as no Ray this gateway knows does."""
        name = cluster["metadata"]["name"]
        try:
            tasks = await self.head_get(
                name,
                "/api/v0/tasks",
                {
                    "filter_keys": "state",
                    "filter_predicates": "=",
                    "filter_values": "RUNNING",
                    "limit": "1",
                },
            )
            jobs = await self.head_get(name, "/api/jobs/")
            # A job's own process is no task: only its status tells it runs.
            return bool(tasks["data"]["result"]["result"]) or any(
                job.get("type") == "SUBMISSION"
                and job.get("status") in ("PENDING", "RUNNING")
                for job in jobs
            )
        except ClientResponseError as e:
            if e.status in (401, 403):
                # Made under another service token: nothing can reach it any more.
                return False
            return head_ready(cluster)
        # The head runs whatever Ray its environment has, whose answers may differ.
        except (ClientError, asyncio.TimeoutError, LookupError, TypeError, ValueError):
            return head_ready(cluster)

    async def head_get(
        self, cluster: str, path: str, params: dict[str, str] | None = None
    ) -> Any:
        async with self.http.get(
            head_url(cluster) + path,
            params=params,
            headers=self.cluster_auth(cluster),
            timeout=ClientTimeout(total=10),
            raise_for_status=True,
        ) as r:
            return await r.json()

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


def dashboard_app(gateway: Gateway) -> web.Application:
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", gateway.handle)
    return app


async def serve() -> None:
    kube_tls = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    async with (
        # Unlimited: a relayed log tail holds its connection for as long as its job runs.
        ClientSession(connector=TCPConnector(limit=0)) as http,
        ClientSession(connector=TCPConnector(ssl=kube_tls)) as kube,
    ):
        gateway = Gateway(http, kube, SERVICE_TOKEN_FILE)
        server = grpc.aio.server(options=GRPC_OPTIONS)
        server.add_generic_rpc_handlers((Relay(gateway),))
        server.add_insecure_port(f"0.0.0.0:{CLIENT_PORT}")
        await server.start()
        # Within the pod's grace period, open log tails or not.
        dashboard = web.AppRunner(
            dashboard_app(gateway), access_log=None, shutdown_timeout=5
        )
        await dashboard.setup()
        await web.TCPSite(dashboard, "0.0.0.0", DASHBOARD_PORT).start()
        try:
            await reap_forever(gateway)
        finally:
            await server.stop(None)
            await dashboard.cleanup()
            await gateway.close()


async def until_changed(path: Path, running: bytes) -> None:
    """Return once kubelet has swapped other content than `running` into the mounted ConfigMap."""
    while path.read_bytes() == running:
        await asyncio.sleep(CODE_POLL_S)
    log.info("%s changed: exiting, for the container to restart on it", path)


async def main() -> None:
    """Serve until SIGTERM, which Python as a container's PID 1 would otherwise ignore,
    or until this file or its settings change."""
    serving = asyncio.ensure_future(serve())
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, serving.cancel)
    changed = [
        asyncio.ensure_future(until_changed(path, path.read_bytes()))
        for path in (CODE, CONFIG_FILE)
    ]
    for change in changed:
        change.add_done_callback(lambda _: serving.cancel())
    with contextlib.suppress(asyncio.CancelledError):
        await serving
    for change in changed:
        change.cancel()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(main())
