"""Ray gateway: each AF user's Ray Client, `ray.init("ray://ray-train-gateway:10001")`,
reaches a Ray cluster of their own, and each job their Jobs API client,
`JobSubmissionClient("http://ray-train-gateway:8265")`, submits runs in a
RayJob of its own: a cluster made the same way, which KubeRay removes once the
job ends and its logs were kept for the idle timeout. The RayJob keeps the job's
status for jobRecordSeconds.

A session's RAY_AUTH_TOKEN is its JupyterHub token, which both clients send
with every call. The Hub says whose it is, and the call goes on, unread, to
that user's RayCluster: created from raycluster.yaml when they first connect,
running as them, and deleted once idle. Only the gateway
holds a cluster's own Ray token. A cluster runs the global Pixi environment,
or the one a call names in `af-env`, with one worker, as many as
`af-n-workers` asks or up to as many as `af-max-workers` asks, each with one
GPU or as many as `af-gpus-per-worker` asks, of the memory
`af-min-gpu-memory-gb` asks or more, and with the cores and memory
`af-cpus-per-worker` and `af-ram-per-worker-gb` ask; a call with any other
`af-` name is refused. A cluster of `af-n-workers` starts only while that many
such GPUs are free, and one of `af-max-workers` with the workers whose GPUs
are; one that a single GPU starts takes it from another cluster's preemptible
worker while none is free. Its pods read the most workers it has from
AF_MAX_WORKERS. A user whose session holds a GPU starts and submits to none.
A workload account's token gets the account a cluster of its own
(../dask-gateway/README.md#workload-accounts). `GET /af/nrp-gpus` on the Jobs
API's port answers how many GPUs NRP Nautilus has free for Ray workers, by
region (nrp_gpus.py). The settings are in config.yaml.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import copy
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import posixpath
import re
import secrets
import signal
import ssl
import string
import time
import urllib.parse
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import grpc
import nrp_jobs
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
from nrp_gpus import as_dict, free_gpus, most_in_one_group, placement, worker_of
from nrp_gpus import nodes as nrp_nodes
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
JOB_RECORD_S = float(CONFIG["jobRecordSeconds"])


@dataclass(frozen=True)
class Flavor:
    """A kind of GPU a worker may hold."""

    resource: str
    memory: float
    budget: int
    # The most of them one worker may hold.
    per_worker: int

    @property
    def group(self) -> str:
        """The name of its worker group: `gpu` for nvidia.com/gpu."""
        return re.sub(r"[^a-z0-9]+", "-", self.resource.rpartition("/")[2])


FLAVORS = [
    Flavor(
        str(g["resource"]),
        float(g["memoryGb"]),
        int(g["budget"]),
        int(g["maxPerWorker"]),
    )
    for g in CONFIG["gpus"]
]
WORKER_CPUS_MAX = int(CONFIG["workerCpusMax"])
WORKER_MEMORY_GB_MIN = int(CONFIG["workerMemoryGbMin"])
WORKER_MEMORY_GB_MAX = int(CONFIG["workerMemoryGbMax"])
PREEMPTIBLE_PRIORITY_CLASS = str(CONFIG["preemptiblePriorityClass"])
PROMETHEUS_URL = str(CONFIG["prometheusUrl"])
NRP_PROMETHEUS_URL = str(CONFIG["nrpPrometheusUrl"])
NRP_GPU_BUDGET = int(CONFIG["nrpGpuBudget"])
NRP_START_TIMEOUT_S = float(CONFIG["nrpStartTimeoutSeconds"])
# Beside this file in the ConfigMap, as in the repository.
NRP_TEMPLATE = CODE.with_name("rayjob-nrp.yaml")
NRP_WORKER = worker_of(yaml.safe_load(NRP_TEMPLATE.read_text()))
# The interLink plugin's, from the kubeconfig-nrp-interlink Secret: without it, no job runs on NRP.
NRP_KUBECONFIG = Path("/etc/nrp/kubeconfig.yaml")
DEFAULT_ENV = str(CONFIG["defaultEnv"])
LDAP_HOST = str(CONFIG["ldapHost"])
LDAP_BASE = str(CONFIG["ldapBase"])
ACCOUNT_KEPT_VOLUMES = {str(name) for name in CONFIG["accountKeptVolumes"]}
# This service's own Hub token, from the `hub` Secret: the file appears once the Hub registers the service.
SERVICE_TOKEN_FILE = Path("/etc/hub-token/token")
# Workload accounts' declarations, from the af-accounts ConfigMap: read on every call.
ACCOUNTS_DIR = Path("/etc/af-accounts")
ACCOUNT_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
ACCOUNT_IDS = range(1000, 2000)
# Top-level directories, the AF's storage roots, PVCs and image user, and volumes of the clusters' pods.
RESERVED_NAMES = frozenset(
    "bin boot dev etc home lib lib32 lib64 libx32 media mnt opt proc root run sbin srv sys tmp usr var "
    "work depot eos cvmfs af-shared-storage hub-db-dir jovyan dask-credentials log-volume".split()
)
# The Hub's per-user PVCs.
RESERVED_PREFIXES = ("claim-",)
TOKEN_SHA256 = re.compile(r"[0-9a-f]{64}")
KUBE_API = "https://kubernetes.default.svc"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
RAYCLUSTERS = f"/apis/ray.io/v1/namespaces/{NAMESPACE}/rayclusters"
RAYJOBS = f"/apis/ray.io/v1/namespaces/{NAMESPACE}/rayjobs"
SECRETS = f"/api/v1/namespaces/{NAMESPACE}/secrets"
MANAGED_BY = "ray-train-gateway"
# The label KubeRay gives the cluster of a RayJob, which KubeRay, not the reaper, removes.
FROM_CRD = "ray.io/originated-from-crd"
# Whose a job is: the name of their own cluster.
OWNER_LABEL = "purdue-af/ray-owner"
ENTRYPOINT_ANNOTATION = "purdue-af/ray-entrypoint"
# Why the gateway stopped a job, when it did.
STOPPED_ANNOTATION = "purdue-af/ray-stopped"
JOB_PATH = re.compile(r"/api/jobs/([^/]+)(/.*)?")
SUBMISSION_ALPHABET = string.ascii_letters + string.digits
# The label of a workload account's cluster's pods, which names the account.
PART_OF = "app.kubernetes.io/part-of"
# Ray Client's port, on the gateway as on every head.
CLIENT_PORT = 10001
# The dashboard's, whose HTTP API, the Jobs API's among it, the gateway relays.
DASHBOARD_PORT = 8265
# What the name of every setting starts with: any other header or metadata so named is a mistake.
SETTING_PREFIX = "af-"
# Set by a notebook: ray.init(..., _metadata=[(ENV_METADATA, <environment>)]), or a header of JobSubmissionClient.
ENV_METADATA = SETTING_PREFIX + "env"
ENV_ANNOTATION = "purdue-af/ray-env"
# Set as the environment is: the cluster's workers, and the GPUs of each.
WORKERS_METADATA = SETTING_PREFIX + "n-workers"
WORKERS_ANNOTATION = "purdue-af/ray-workers"
# Set in place of the workers: the most of them, of which a cluster starts with those whose GPUs are free.
MOST_METADATA = SETTING_PREFIX + "max-workers"
LEAST_ANNOTATION = "purdue-af/ray-min-workers"
# Where a cluster's pods read the most workers it has: what a training sizes itself by.
MOST_VARIABLE = "AF_MAX_WORKERS"
PER_WORKER_METADATA = SETTING_PREFIX + "gpus-per-worker"
PER_WORKER_ANNOTATION = "purdue-af/ray-gpus-per-worker"
# Set as the environment is: the least memory a worker's GPU may have, in GB.
MEMORY_METADATA = SETTING_PREFIX + "min-gpu-memory-gb"
MEMORY_ANNOTATION = "purdue-af/ray-gpu-memory"
# Set as the environment is: the CPU cores of each worker, and its memory in GB.
CPUS_METADATA = SETTING_PREFIX + "cpus-per-worker"
CPUS_ANNOTATION = "purdue-af/ray-worker-cpus"
RAM_METADATA = SETTING_PREFIX + "ram-per-worker-gb"
# Set as the environment is, by a job: where it runs.
SITE_METADATA = SETTING_PREFIX + "site"
SITES = ("purdue", "nrp")
SETTINGS = {
    ENV_METADATA,
    WORKERS_METADATA,
    MOST_METADATA,
    PER_WORKER_METADATA,
    MEMORY_METADATA,
    CPUS_METADATA,
    RAM_METADATA,
    SITE_METADATA,
}
# What a job on NRP may set: its workers are those of rayjob-nrp.yaml, in its image's environment.
NRP_SETTINGS = {WORKERS_METADATA, MOST_METADATA, SITE_METADATA}
# A setting's name without its prefix is no header of Ray's either.
UNPREFIXED = {name.removeprefix(SETTING_PREFIX) for name in SETTINGS}
RAM_ANNOTATION = "purdue-af/ray-worker-memory"
REMOVED_IDLE = "Your Ray cluster was removed after it went idle: run ray.shutdown(), then ray.init() again."
REMOVED_UNPROVISIONED = "Your Ray cluster was removed, as not all of its GPUs started in time: ask for fewer workers, or try again later."
STOPPED_UNPROVISIONED = "The job was stopped, as not all of its GPUs started in time: ask for fewer workers, or try again later."
JOB_STARTING = "Its Ray cluster is starting."
# singleuser.podNameTemplate in the Hub values
SESSION_POD = re.compile(r"purdue-af-(\d+)")
# custom-spawner.py names the accounts from outside Purdue <login>-cern and <login>-fnal
EXTERNAL_SUFFIXES = ("-cern", "-fnal")
# KubeRay's longest RayCluster name: the Services it names after one must fit in 63 characters.
MAX_CLUSTER_NAME = 53
DNS_LABEL = re.compile(r"[a-z]([-a-z0-9]*[a-z0-9])?")
# An account's cluster is ray-train-<name>, as cluster_name gives a Hub user of its name.
ACCOUNT_NAME_MAX = MAX_CLUSTER_NAME - len("ray-train-")
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
NOT_RELAYED = {"authorization", "user-agent"} | SETTINGS
# Headers a relayed HTTP call carries on neither way: the gateway's own, and its connections' framing.
NOT_PASSED_ON = {
    "authorization",
    "host",
    *SETTINGS,
    "connection",
    "keep-alive",
    "upgrade",
    "transfer-encoding",
    "content-length",
}
# A long log tail or upload is no failure; only a head that cannot be reached is.
RELAY_TIMEOUT = ClientTimeout(total=None, sock_connect=10)
# How long a refusal waits for the call's first request, which a Ray client sends at once.
REFUSAL_READ_S = 5.0
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
    # Whether their session holds a GPU of its own.
    session_gpu: bool = False
    # Whether it is a workload account rather than a Hub user.
    workload: bool = False

    @property
    def cluster(self) -> str:
        return cluster_name(self.name)

    @property
    def owner(self) -> str:
        """The workload account their cluster is of, or "" for a Hub user's."""
        return self.name if self.workload else ""


@dataclass(frozen=True)
class Account:
    """A workload account, as its declaration states it."""

    name: str
    uid: int
    gid: int
    token_sha256: str


class AccountRefused(Exception):
    """A declaration that breaks a rule, which the exception names."""


@dataclass(frozen=True)
class Shape:
    """What a user's cluster runs: an environment, the most workers it has, the
    memory of the smallest GPU it may hold, in GB, the CPU cores, the memory,
    in GB, and the GPUs of each worker, and the fewest workers it starts with."""

    env: str
    workers: str
    memory: str
    cpus: str
    ram: str
    per_worker: str
    least: str
    site: str = "purdue"

    @property
    def flavors(self) -> list[Flavor]:
        return [
            f
            for f in FLAVORS
            if f.memory >= float(self.memory) and f.per_worker >= int(self.per_worker)
        ]


@dataclass(frozen=True)
class Waiting:
    """A RayJob whose head is up and waits for its job: what a submission's package
    uploads reach, and then the submission itself."""

    name: str
    head: str
    shape: Shape
    since: float


def cluster_name(username: str) -> str:
    """ray-train-<username>, or for a username that cannot be part of a
    Kubernetes name, what can stay of it and a hash that keeps it apart."""
    name = f"ray-train-{username}"
    if len(name) <= MAX_CLUSTER_NAME and DNS_LABEL.fullmatch(name):
        return name
    stem = re.sub(r"[^a-z0-9]+", "-", username.lower()).strip("-")
    suffix = "---" + hashlib.sha256(username.encode()).hexdigest()[:8]
    return f"ray-train-{stem}"[: MAX_CLUSTER_NAME - len(suffix)] + suffix


def checked_account(declared: Any) -> Account:
    """The account a declaration states, if it keeps the rules."""
    if not isinstance(declared, dict) or set(declared) != {
        "name",
        "uid",
        "gid",
        "tokenSha256",
    }:
        raise AccountRefused("keys")
    name = declared["name"]
    if (
        not isinstance(name, str)
        or len(name) > ACCOUNT_NAME_MAX
        or not ACCOUNT_NAME.fullmatch(name)
    ):
        raise AccountRefused("name")
    if (
        name in RESERVED_NAMES
        or name.startswith(RESERVED_PREFIXES)
        or name.endswith(EXTERNAL_SUFFIXES)
    ):
        raise AccountRefused("reserved-name")
    for key in ("uid", "gid"):
        if type(declared[key]) is not int or declared[key] not in ACCOUNT_IDS:
            raise AccountRefused(key)
    token_sha256 = declared["tokenSha256"]
    if not isinstance(token_sha256, str) or not TOKEN_SHA256.fullmatch(token_sha256):
        raise AccountRefused("token-hash")
    return Account(name, declared["uid"], declared["gid"], token_sha256)


def read_accounts(paths: list[Path]) -> dict[str, Account]:
    """The valid declarations among `paths`, by name; the others are logged and left out."""
    found: list[Account] = []
    for path in paths:
        try:
            found.append(checked_account(yaml.safe_load(path.read_text())))
        except AccountRefused as refused:
            log.warning("workload account %s refused: reason=%s", path.name, refused)
        except Exception:
            log.warning("workload account %s refused: reason=unreadable", path.name)
    names = collections.Counter(a.name for a in found)
    hashes = collections.Counter(a.token_sha256 for a in found)
    valid: dict[str, Account] = {}
    for account in found:
        if names[account.name] > 1 or hashes[account.token_sha256] > 1:
            log.warning("workload account %s refused: reason=duplicate", account.name)
        else:
            valid[account.name] = account
    return valid


_accounts: tuple[list[tuple[Path, int, int, int]] | None, dict[str, Account]] = (
    None,
    {},
)


def accounts() -> dict[str, Account]:
    """The valid workload accounts by name, read again when a declaration changes; none without ACCOUNTS_DIR."""
    global _accounts
    stamp = []
    try:
        paths = sorted(ACCOUNTS_DIR.glob("*.yaml"))
    except OSError:
        paths = []
    for path in paths:
        try:
            found = path.stat()
        except OSError:
            continue
        stamp.append((path, found.st_ino, found.st_mtime_ns, found.st_size))
    if stamp != _accounts[0]:
        _accounts = (stamp, read_accounts([entry[0] for entry in stamp]))
    return _accounts[1]


def token_account(token: str) -> Account | None:
    """The workload account whose token this is, or None."""
    # A header that is no UTF-8 reaches here with its bytes as surrogates.
    digest = hashlib.sha256(token.encode("utf-8", "surrogateescape")).hexdigest()
    owner = None
    for account in accounts().values():
        if hmac.compare_digest(digest, account.token_sha256):
            owner = account
    return owner


def refuse_account_name(name: str) -> None:
    """Refuses a Hub user whose cluster would be a workload account's."""
    if cluster_name(name) in {cluster_name(a) for a in accounts()}:
        log.warning("Hub user refused: reason=account-name account=%s", name)
        raise Refused(
            grpc.StatusCode.PERMISSION_DENIED,
            f"{name} is the name of a workload account: no Ray cluster runs for a Hub user of it.",
        )


def bearer_token(header: str) -> str | None:
    scheme, _, token = header.partition(" ")
    token = token.strip()
    return token if scheme.lower() in ("bearer", "token") and token else None


def session_pod(user_model: dict[str, Any]) -> re.Match[str] | None:
    """The pod name of the user's running session, and the AF id in it."""
    server = (user_model.get("servers") or {}).get("") or {}
    pod_name = (server.get("state") or {}).get("pod_name", "")
    return SESSION_POD.fullmatch(pod_name)


def session_gpus_query(pod: str) -> str:
    """The GPUs a session pod asks for, of every kind a session may hold."""
    resources = "|".join(GPU_METRICS.values())
    return (
        "sum by (resource) (kube_pod_container_resource_requests"
        f'{{namespace="{NAMESPACE}",pod="{pod}",resource=~"{resources}"}})'
    )


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
            WORKERS_ANNOTATION: shape.workers,
            MEMORY_ANNOTATION: shape.memory,
            CPUS_ANNOTATION: shape.cpus,
            RAM_ANNOTATION: shape.ram,
            PER_WORKER_ANNOTATION: shape.per_worker,
            LEAST_ANNOTATION: shape.least,
        }
    )
    spec = cluster["spec"]
    spec["authOptions"]["secretName"] = user.cluster
    (group,) = spec["workerGroupSpecs"]
    for container in group["template"]["spec"]["containers"]:
        for amounts in container["resources"].values():
            amounts.update(cpu=shape.cpus, memory=f"{shape.ram}Gi")
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
            variables.append({"name": MOST_VARIABLE, "value": shape.workers})
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
                    worker_group(
                        group, flavor, int(shape.per_worker), replicas, preemptible
                    )
                )
        kept = max(kept - workers, 0)
    return cluster


def worker_group(
    template: dict[str, Any],
    flavor: Flavor,
    per_worker: int,
    replicas: int,
    preemptible: bool,
) -> dict[str, Any]:
    """The template's worker group, as `replicas` workers with `per_worker` GPUs of `flavor` each."""
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
            amounts[flavor.resource] = per_worker
    return group


def token_secret(name: str, kind: str, owner_uid: str, token: str) -> dict[str, Any]:
    """KubeRay reads the cluster's token from it; it goes with the RayCluster or RayJob `name`."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "labels": {"app.kubernetes.io/managed-by": MANAGED_BY},
            "ownerReferences": [
                {
                    "apiVersion": "ray.io/v1",
                    "kind": kind,
                    "name": name,
                    "uid": owner_uid,
                }
            ],
        },
        "stringData": {"auth_token": token},
    }


def new_submission() -> str:
    """A submission ID as Ray makes one."""
    return "raysubmit_" + "".join(
        secrets.choice(SUBMISSION_ALPHABET) for _ in range(16)
    )


def job_name(user: User, key: str) -> str:
    """A RayJob of the user's, made before its job is known: short enough for the cluster KubeRay names after it."""
    digest = hashlib.sha256(f"{user.owner}/{user.cluster}/{key}".encode()).hexdigest()
    return f"ray-job-{digest[:16]}"


def build_job(
    template: dict[str, Any],
    user: User,
    shape: Shape,
    mix: dict[str, int],
    name: str,
) -> dict[str, Any]:
    """A RayJob whose cluster is the one build_cluster makes, which waits until
    the gateway sets the jobId of the job it submits, and goes once that job
    ends and its logs were kept for the idle timeout."""
    cluster = build_cluster(template, user, shape, mix)
    cluster["spec"]["authOptions"]["secretName"] = name
    metadata = cluster["metadata"]
    return {
        "apiVersion": "ray.io/v1",
        "kind": "RayJob",
        "metadata": {
            "name": name,
            # KubeRay gives them to the cluster too, which admission then counts.
            "labels": {**metadata["labels"], OWNER_LABEL: user.cluster},
            "annotations": metadata["annotations"],
        },
        "spec": {
            "submissionMode": "InteractiveMode",
            "shutdownAfterJobFinishes": True,
            "ttlSecondsAfterFinished": int(IDLE_TIMEOUT_S),
            "rayClusterSpec": cluster["spec"],
        },
    }


def epoch_ms(stamp: str | None) -> int | None:
    return int(datetime.fromisoformat(stamp).timestamp() * 1000) if stamp else None


def job_ended(job: dict[str, Any]) -> bool:
    return (job.get("status") or {}).get("jobStatus") in (
        "SUCCEEDED",
        "FAILED",
        "STOPPED",
    )


def owns(user: User, job: dict[str, Any]) -> bool:
    """Whether the job is the user's: of their cluster's name, and running as they do, person or account."""
    labels = job["metadata"].get("labels") or {}
    cluster = {"spec": job["spec"]["rayClusterSpec"]}
    return (
        labels.get(OWNER_LABEL) == user.cluster and cluster_owner(cluster) == user.owner
    )


def job_details(job: dict[str, Any]) -> dict[str, Any]:
    """The job as the Jobs API reports one, from its RayJob: what is left once its cluster is gone."""
    status = job.get("status") or {}
    ran = status.get("rayJobInfo") or {}
    annotations = job["metadata"].get("annotations") or {}
    stopped = annotations.get(STOPPED_ANNOTATION)
    return {
        "type": "SUBMISSION",
        "job_id": None,
        "submission_id": job["spec"]["jobId"],
        "driver_info": None,
        "status": status.get("jobStatus") or "PENDING",
        "entrypoint": annotations.get(ENTRYPOINT_ANNOTATION, ""),
        "message": stopped or status.get("message") or JOB_STARTING,
        "error_type": None,
        "start_time": epoch_ms(ran.get("startTime") or status.get("startTime")),
        "end_time": epoch_ms(ran.get("endTime") or status.get("endTime")),
        "metadata": {},
        "runtime_env": {},
        "driver_agent_http_address": None,
        "driver_node_id": None,
        "driver_exit_code": None,
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
    """The GPUs the cluster's workers have of each resource."""
    held: dict[str, int] = {}
    for group in cluster["spec"].get("workerGroupSpecs") or []:
        for container in group["template"]["spec"]["containers"]:
            limits = (container.get("resources") or {}).get("limits") or {}
            for flavor in FLAVORS:
                if flavor.resource in limits:
                    held[flavor.resource] = held.get(flavor.resource, 0) + int(
                        group.get("replicas", 0)
                    ) * int(limits[flavor.resource])
    return held


def load_template() -> dict[str, Any]:
    template: dict[str, Any] = yaml.safe_load(TEMPLATE.read_text())
    return template


def cluster_template(user: User) -> dict[str, Any]:
    """The template of the user's cluster: a workload account's keeps
    ACCOUNT_KEPT_VOLUMES of its volumes and mounts its own at /<name>."""
    template = load_template()
    if not user.workload:
        return template
    volume = {"name": user.name, "persistentVolumeClaim": {"claimName": user.name}}
    mount = {"name": user.name, "mountPath": f"/{user.name}"}
    spec = template["spec"]
    for pod in (
        spec["headGroupSpec"]["template"],
        *(group["template"] for group in spec["workerGroupSpecs"]),
    ):
        pod["metadata"]["labels"][PART_OF] = user.name
        pod["spec"]["volumes"] = [
            v for v in pod["spec"]["volumes"] if v["name"] in ACCOUNT_KEPT_VOLUMES
        ] + [volume]
        for container in pod["spec"]["containers"]:
            container["volumeMounts"] = [
                m
                for m in container["volumeMounts"]
                if m["name"] in ACCOUNT_KEPT_VOLUMES
            ] + [mount]
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
    return mounted_env(value, template)


def mounted_env(value: str, template: dict[str, Any]) -> str:
    """The environment at `value`, on storage the template's cluster mounts."""
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


def worker_size(template: dict[str, Any]) -> tuple[str, str]:
    """The CPU cores and the memory, in GB, of the template's worker."""
    (group,) = template["spec"]["workerGroupSpecs"]
    (container,) = group["template"]["spec"]["containers"]
    limits = container["resources"]["limits"]
    return str(limits["cpu"]), str(limits["memory"]).removesuffix("Gi")


def requested_size(name: str, value: str, default: str, least: int, most: int) -> str:
    """What a call asks each worker to have of CPU cores or memory, or the template's."""
    if not value:
        return default
    if value in {str(n) for n in range(least, most + 1)}:
        return value
    raise Refused(
        grpc.StatusCode.INVALID_ARGUMENT,
        f"{name} is a whole number from {least} to {most}.",
    )


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


def requested_per_worker(value: str, memory: str) -> str:
    """The GPUs a call asks each worker to have, one by default."""
    if not value:
        return "1"
    most = max(f.per_worker for f in FLAVORS if f.memory >= float(memory))
    if value in {str(n) for n in range(1, most + 1)}:
        return value
    raise Refused(
        grpc.StatusCode.INVALID_ARGUMENT,
        f"{PER_WORKER_METADATA} is a number of GPUs from 1 to {most}.",
    )


def requested_workers(exactly: str, at_most: str, shape: Shape) -> tuple[str, str]:
    """The most workers a call asks for and the fewest its cluster starts with:
    all of `exactly`, or one of up to `at_most`; one worker by default."""
    if exactly and at_most:
        raise Refused(
            grpc.StatusCode.INVALID_ARGUMENT,
            f"{WORKERS_METADATA} asks for a number of workers and {MOST_METADATA} for up to as many: set one of them.",
        )
    value = exactly or at_most
    if not value:
        return "1", "1"
    most = sum(f.budget // int(shape.per_worker) for f in shape.flavors)
    if value in {str(n) for n in range(1, most + 1)}:
        return value, exactly or "1"
    raise Refused(
        grpc.StatusCode.INVALID_ARGUMENT,
        f"{WORKERS_METADATA if exactly else MOST_METADATA} is a number of workers from 1 to {most}.",
    )


def nrp_workers(exactly: str, at_most: str) -> tuple[str, str]:
    """As requested_workers, for the workers of a job on NRP."""
    if exactly and at_most:
        raise Refused(
            grpc.StatusCode.INVALID_ARGUMENT,
            f"{WORKERS_METADATA} asks for a number of workers and {MOST_METADATA} for up to as many: set one of them.",
        )
    value = exactly or at_most
    if not value:
        return "1", "1"
    most = NRP_GPU_BUDGET // NRP_WORKER.gpus
    if value in {str(n) for n in range(1, most + 1)}:
        return value, exactly or "1"
    raise Refused(
        grpc.StatusCode.INVALID_ARGUMENT,
        f"{WORKERS_METADATA if exactly else MOST_METADATA} is a number of workers from 1 to {most} on NRP.",
    )


async def submitted_json(request: web.Request) -> dict[str, Any]:
    try:
        submitted = json.loads(await request.read())
    except ValueError:
        submitted = None
    if not isinstance(submitted, dict):
        raise Refused(
            grpc.StatusCode.INVALID_ARGUMENT, "A job submission is a JSON object."
        )
    return submitted


def cluster_shape(cluster: dict[str, Any]) -> Shape:
    annotations = cluster["metadata"].get("annotations") or {}
    cpus, ram = worker_size(load_template())
    workers = str(annotations.get(WORKERS_ANNOTATION, ""))
    return Shape(
        str(annotations.get(ENV_ANNOTATION, "")),
        workers,
        # Without it, a cluster holds GPUs that any call may have.
        str(annotations.get(MEMORY_ANNOTATION, requested_memory(""))),
        # Without them, its workers are the template's.
        str(annotations.get(CPUS_ANNOTATION, cpus)),
        str(annotations.get(RAM_ANNOTATION, ram)),
        str(annotations.get(PER_WORKER_ANNOTATION, "1")),
        # Without it, a cluster started with all its workers.
        str(annotations.get(LEAST_ANNOTATION, workers)),
    )


def cluster_owner(cluster: dict[str, Any]) -> str:
    """Whose a cluster is, as User.owner states it."""
    head = cluster["spec"]["headGroupSpec"]["template"]
    return str(((head.get("metadata") or {}).get("labels") or {}).get(PART_OF, ""))


def refuse_others(cluster: dict[str, Any], user: User) -> None:
    """Refuses the user a cluster of their cluster's name that is someone else's."""
    if cluster_owner(cluster) != user.owner:
        log.warning("%s refused: reason=owner", user.cluster)
        raise Refused(
            grpc.StatusCode.PERMISSION_DENIED,
            f"{user.cluster} is someone else's Ray cluster.",
        )


def runs(cluster: dict[str, Any], shape: Shape) -> bool:
    """Whether the cluster stays up and is of `shape`."""
    return (
        not cluster["metadata"].get("deletionTimestamp")
        and cluster_shape(cluster) == shape
    )


class Gateway:
    def __init__(
        self,
        http: ClientSession,
        kube: ClientSession,
        token_file: Path,
        nrp: nrp_jobs.NrpApi | None = None,
        bucket: nrp_jobs.Bucket | None = None,
    ) -> None:
        self.http = http
        # Without either, no job runs on NRP.
        self.nrp = nrp
        self.bucket = bucket
        self.kube_http = kube
        self.token_file = token_file
        self.last_used: dict[str, float] = {}
        # Whose each cluster was last found to be, and in what shape, so that a call skips the checks.
        self.started: dict[str, tuple[str, Shape]] = {}
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
        # user's cluster -> their job that waits for its submission, and the lock that makes one at a time
        self.waiting: dict[str, Waiting] = {}
        self.submitting: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        # user's cluster -> the RayJob on NRP whose code their submission uploads, and since when
        self.nrp_waiting: dict[str, tuple[str, float]] = {}
        # Jobs on NRP whose code is deleted, until their records are
        self.nrp_cleaned: set[str] = set()
        self.nrp_admitting = asyncio.Lock()

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
            # Refusing ahead of the request races the client's write of it, which grpc reports as INTERNAL.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(anext(requests, None), REFUSAL_READ_S)
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
        unknown = sorted(
            name
            for name in values
            if name in UNPREFIXED
            or (name.startswith(SETTING_PREFIX) and name not in SETTINGS)
        )
        if unknown:
            raise Refused(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"{', '.join(unknown)} is no setting of a Ray cluster: the settings are {', '.join(sorted(SETTINGS))}.",
            )
        site = values.get(SITE_METADATA) or "purdue"
        if site not in SITES:
            raise Refused(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"{SITE_METADATA} is one of {', '.join(SITES)}.",
            )
        user = await self.user_for(token)
        if site == "nrp":
            others = sorted(name for name in values if name in SETTINGS - NRP_SETTINGS)
            if others:
                raise Refused(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"{', '.join(others)} sets a Ray cluster at Purdue: a job on NRP runs the AF's global environment on workers of {NRP_WORKER.gpus} GPU, {NRP_WORKER.cpus:g} CPUs and {NRP_WORKER.memory_gb:g} GB, and takes only {WORKERS_METADATA} or {MOST_METADATA}.",
                )
            workers, least = nrp_workers(
                values.get(WORKERS_METADATA, ""), values.get(MOST_METADATA, "")
            )
            return user, Shape("", workers, "", "", "", "1", least, site)
        template = cluster_template(user)
        cpus, ram = worker_size(template)
        memory = requested_memory(values.get(MEMORY_METADATA, ""))
        shape = Shape(
            requested_env(values.get(ENV_METADATA, ""), template),
            "1",
            memory,
            requested_size(
                CPUS_METADATA, values.get(CPUS_METADATA, ""), cpus, 1, WORKER_CPUS_MAX
            ),
            requested_size(
                RAM_METADATA,
                values.get(RAM_METADATA, ""),
                ram,
                WORKER_MEMORY_GB_MIN,
                WORKER_MEMORY_GB_MAX,
            ),
            requested_per_worker(values.get(PER_WORKER_METADATA, ""), memory),
            "1",
        )
        workers, least = requested_workers(
            values.get(WORKERS_METADATA, ""), values.get(MOST_METADATA, ""), shape
        )
        return user, dataclasses.replace(shape, workers=workers, least=least)

    async def cluster_for(self, user: User, shape: Shape) -> str:
        """The user's cluster, started in `shape` unless it runs in it."""
        if shape.site != "purdue":
            raise Refused(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"{SITE_METADATA}: {shape.site} is for jobs a JobSubmissionClient submits: Ray Client reaches your own cluster at Purdue.",
            )
        if user.session_gpu:
            raise Refused(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Your session holds a GPU, and Ray clusters are for sessions without one: train on your session's GPU, or start a session without a GPU.",
            )
        # Here rather than in caller: asking after jobs needs no environment.
        mounted_env(shape.env, cluster_template(user))
        if self.started.get(user.cluster) != (user.owner, shape):
            await self.start_cluster(user, shape)
            self.started[user.cluster] = (user.owner, shape)
        self.last_used[user.cluster] = time.monotonic()
        return user.cluster

    async def running(self, user: User) -> str:
        """The user's cluster in whatever shape: asking about jobs starts none."""
        known = self.started.get(user.cluster)
        if known is None or known[0] != user.owner:
            cluster = await self.cluster(user.cluster)
            if cluster is None or cluster["metadata"].get("deletionTimestamp"):
                raise Refused(
                    grpc.StatusCode.NOT_FOUND,
                    self.never_started.get(
                        user.cluster,
                        "You have no Ray cluster: one is removed once idle, with the records and logs of its jobs.",
                    ),
                )
            refuse_others(cluster, user)
        self.last_used[user.cluster] = time.monotonic()
        return user.cluster

    async def submission(
        self, user: User, shape: Shape, request: web.Request
    ) -> tuple[str, str, bytes | None]:
        """The head a submission or one of its uploads goes to, the job whose token
        it carries, and a submission's body, which names the job that head waits for."""
        if user.session_gpu:
            raise Refused(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Your session holds a GPU, and Ray jobs are for sessions without one: train on your session's GPU, or start a session without a GPU.",
            )
        mounted_env(shape.env, cluster_template(user))
        submitted: dict[str, Any] | None = None
        if request.method == "POST":
            submitted = await submitted_json(request)
        async with self.submitting[user.cluster]:
            if submitted is not None:
                asked = str(submitted.get("submission_id") or new_submission())
                if await self.submitted_before(user, asked):
                    raise Refused(
                        grpc.StatusCode.FAILED_PRECONDITION,
                        f"You have a job {asked} already: submit it under another ID, or without one.",
                    )
            waiting = self.waiting.pop(user.cluster, None)
            if waiting is not None and (
                waiting.shape != shape
                or time.monotonic() - waiting.since > START_TIMEOUT_S
            ):
                # The reaper removes what no submission came to.
                waiting = None
            if waiting is None:
                waiting = await self.start_job(user, shape)
            if submitted is None:
                self.waiting[user.cluster] = waiting
                return waiting.head, waiting.name, None
            # KubeRay follows the job whose jobId is set on a RayJob that waits for one.
            submitted["submission_id"] = asked
            entrypoint = str(submitted.get("entrypoint", ""))[:4096]
            await self.kube(
                "PATCH",
                f"{RAYJOBS}/{waiting.name}",
                {
                    "metadata": {"annotations": {ENTRYPOINT_ANNOTATION: entrypoint}},
                    "spec": {"jobId": asked},
                },
            )
        return waiting.head, waiting.name, json.dumps(submitted).encode()

    async def start_job(self, user: User, shape: Shape) -> Waiting:
        """Create a RayJob that waits for its job, and return once its head answers."""
        name = job_name(user, new_submission())
        mix = await self.admit(user, shape, name=name)
        status, created = await self.kube(
            "POST",
            RAYJOBS,
            build_job(cluster_template(user), user, shape, mix, name),
        )
        if status != 201:
            self.grants.pop(name, None)
            raise Refused(
                grpc.StatusCode.UNAVAILABLE,
                f"Could not create your Ray job: {created.get('message', status)}",
            )
        log.info("created %s", name)
        secret = token_secret(
            name,
            "RayJob",
            created["metadata"]["uid"],
            cluster_token(self.service_token, name),
        )
        deadline = time.monotonic() + START_TIMEOUT_S
        try:
            while True:
                status, body = await self.kube("POST", SECRETS, secret)
                if status not in (201, 409):
                    raise Refused(
                        grpc.StatusCode.UNAVAILABLE,
                        f"Could not create your Ray job's token: {body.get('message', status)}",
                    )
                job = await self.rayjob(name)
                head = ((job or {}).get("status") or {}).get("rayClusterName")
                if head and await self.head_status(head, keyed=name) == 200:
                    break
                if time.monotonic() > deadline:
                    raise Refused(
                        grpc.StatusCode.UNAVAILABLE,
                        "Your Ray job's head did not start in time: try again later.",
                    )
                await asyncio.sleep(START_POLL_S)
            # A Ray that ignores RAY_AUTH_MODE would serve anyone who reaches it.
            if await self.head_status(head, authorized=False) == 200:
                raise Refused(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "The Ray in that environment does not enforce the cluster's token.",
                )
        except Refused:
            await self.kube(
                "DELETE", f"{RAYJOBS}/{name}", {"propagationPolicy": "Background"}
            )
            raise
        return Waiting(name, head, shape, time.monotonic())

    async def rayjob(self, name: str) -> dict[str, Any] | None:
        status, body = await self.kube("GET", f"{RAYJOBS}/{name}")
        if status == 404:
            return None
        if status != 200:
            raise Refused(
                grpc.StatusCode.UNAVAILABLE,
                f"Kubernetes answered HTTP {status} for the Ray job.",
            )
        return body

    async def job(
        self, user: User, submission: str
    ) -> tuple[dict[str, Any], str | None] | None:
        """The user's RayJob of `submission`, and its cluster while that is up; None if they have none of it."""
        job = next(
            (j for j in await self.jobs(user) if j["spec"]["jobId"] == submission),
            None,
        )
        if job is None:
            return None
        head = (job.get("status") or {}).get("rayClusterName")
        if head:
            cluster = await self.cluster(head)
            if cluster is None or cluster["metadata"].get("deletionTimestamp"):
                head = None
        return job, head

    async def jobs(self, user: User) -> list[dict[str, Any]]:
        selector = urllib.parse.quote(f"{OWNER_LABEL}={user.cluster}")
        status, listing = await self.kube("GET", f"{RAYJOBS}?labelSelector={selector}")
        if status != 200:
            raise Refused(
                grpc.StatusCode.UNAVAILABLE,
                f"Kubernetes answered HTTP {status} for your Ray jobs.",
            )
        # One that waits for its job yet is none of the user's jobs.
        return [
            job
            for job in listing.get("items", [])
            if owns(user, job) and job["spec"].get("jobId")
        ]

    async def recorded(
        self, job: dict[str, Any], method: str, rest: str
    ) -> web.StreamResponse:
        """A call about a job whose cluster is not up, answered from its RayJob."""
        name = job["metadata"]["name"]
        ended = job_ended(job)
        if method == "GET" and not rest:
            return web.json_response(job_details(job))
        if method == "DELETE" and not rest:
            if not ended:
                text = "The job has not ended: stop it first"
                return web.Response(status=400, reason=text, text=text)
            await self.kube(
                "DELETE", f"{RAYJOBS}/{name}", {"propagationPolicy": "Background"}
            )
            return web.json_response({"deleted": True})
        if method == "POST" and rest == "/stop":
            if not ended:
                # Its cluster never came up: nothing ran, and nothing need wait for it.
                await self.kube(
                    "DELETE", f"{RAYJOBS}/{name}", {"propagationPolicy": "Background"}
                )
            return web.json_response({"stopped": not ended})
        text = (
            f"The job's Ray cluster was removed {int(IDLE_TIMEOUT_S // 60)} minutes after the job ended, and its logs with it"
            if ended
            else "The job's Ray cluster is starting: ask again in a minute"
        )
        return web.Response(status=404, reason=text, text=text)

    async def submitted_before(self, user: User, submission: str) -> bool:
        """Whether the user has a job of that ID, at Purdue or on NRP."""
        if any(j["spec"]["jobId"] == submission for j in await self.jobs(user)):
            return True
        return await self.nrp_job(user, submission) is not None

    async def nrp_submission(
        self, user: User, shape: Shape, request: web.Request
    ) -> web.StreamResponse:
        """A submission to NRP, or an upload of the code it takes along, which goes to
        the S3 prefix of its RayJob: one the gateway creates on NRP at the submission."""
        if self.nrp is None or self.bucket is None:
            raise Refused(
                grpc.StatusCode.UNAVAILABLE,
                "Jobs on NRP are not enabled: the gateway has no NRP kubeconfig or S3 keys.",
            )
        async with self.submitting[user.cluster]:
            name, since = self.nrp_waiting.get(user.cluster, ("", 0.0))
            if not name or time.monotonic() - since > START_TIMEOUT_S:
                name = job_name(user, new_submission())
            package = request.path.rpartition("/")[2]
            if request.path.startswith("/api/packages/"):
                self.nrp_waiting[user.cluster] = (name, time.monotonic())
                key = f"{nrp_jobs.code_prefix(name)}/{package}"
                if request.method == "PUT":
                    await self.bucket.put(key, await request.read())
                    return web.Response()
                return web.Response(
                    status=200 if await self.bucket.exists(key) else 404
                )
            submitted = await submitted_json(request)
            self.nrp_waiting.pop(user.cluster, None)
            asked = str(submitted.get("submission_id") or new_submission())
            if await self.submitted_before(user, asked):
                raise Refused(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    f"You have a job {asked} already: submit it under another ID, or without one.",
                )
            await self.start_nrp_job(
                user, shape, name, submitted | {"submission_id": asked}
            )
        # As Ray answers: its client reads job_id too.
        return web.json_response({"job_id": asked, "submission_id": asked})

    async def start_nrp_job(
        self, user: User, shape: Shape, name: str, submitted: dict[str, Any]
    ) -> None:
        """Create the job's RayJob on NRP, its workers on nodes of one region and GPU model with room for them."""
        assert self.nrp is not None and self.bucket is not None
        async with self.nrp_admitting:
            held = sum(
                nrp_jobs.held_gpus(j)
                for j in await self.nrp_listing()
                if not job_ended(j)
            )
            room = (NRP_GPU_BUDGET - held) // NRP_WORKER.gpus
            wanted, least = int(shape.workers), int(shape.least)
            try:
                found = await nrp_nodes(self.http, NRP_PROMETHEUS_URL, NRP_WORKER)
            except (ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
                raise Refused(
                    grpc.StatusCode.UNAVAILABLE,
                    "NRP's Prometheus did not say which GPUs are free: try again in a minute.",
                ) from e
            chosen = (
                placement(found, min(wanted, room), least) if room >= least else None
            )
            if chosen is None:
                log.info(
                    "refused %s %d workers on NRP: room %d, %d free",
                    name,
                    least,
                    room,
                    most_in_one_group(found),
                )
                raise Refused(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    f"Your job asks for {least} workers on NRP, which has room for {most_in_one_group(found)} of one region and GPU model now, and AF jobs may take {max(room, 0)} more there: ask for fewer, or try again later.",
                )
            workers = min(wanted, room, sum(n.workers for n in chosen))
            job = nrp_jobs.build_job(
                yaml.safe_load(NRP_TEMPLATE.read_text()),
                name,
                nrp_jobs.owner_key(user.cluster, user.owner),
                submitted,
                self.bucket.name,
                workers,
                chosen,
                int(IDLE_TIMEOUT_S),
            )
            status, created = await self.nrp.call("POST", self.nrp.path("rayjobs"), job)
        if status != 201:
            message = (
                created.get("message", status) if isinstance(created, dict) else status
            )
            raise Refused(
                grpc.StatusCode.UNAVAILABLE, f"NRP did not take your job: {message}"
            )
        log.info(
            "created %s on NRP: %d workers on %s, %s",
            name,
            workers,
            chosen[0].region,
            chosen[0].model,
        )

    async def nrp_listing(self, user: User | None = None) -> list[dict[str, Any]]:
        """The gateway's RayJobs on NRP, or the user's."""
        assert self.nrp is not None
        selector = f"app.kubernetes.io/managed-by={MANAGED_BY}"
        if user is not None:
            selector = f"{OWNER_LABEL}={nrp_jobs.owner_key(user.cluster, user.owner)}"
        status, listing = await self.nrp.call(
            "GET",
            f"{self.nrp.path('rayjobs')}?labelSelector={urllib.parse.quote(selector)}",
        )
        if status != 200:
            raise Refused(
                grpc.StatusCode.UNAVAILABLE,
                f"NRP answered HTTP {status} for the Ray jobs.",
            )
        return list(listing.get("items", []))

    async def nrp_jobs(self, user: User) -> list[dict[str, Any]]:
        """The user's jobs on NRP; none, if NRP does not answer, with a warning."""
        if self.nrp is None:
            return []
        try:
            return await self.nrp_listing(user)
        except (Refused, ClientError, asyncio.TimeoutError) as e:
            log.warning("listing jobs on NRP: %r", e)
            return []

    async def nrp_job(self, user: User, submission: str) -> dict[str, Any] | None:
        for job in await self.nrp_jobs(user):
            if job["spec"].get("jobId") == submission:
                return job
        return None

    async def nrp_recorded(
        self, request: web.Request, job: dict[str, Any], rest: str
    ) -> web.StreamResponse:
        """A call about a job on NRP, answered from its RayJob and its submitter's log."""
        assert self.nrp is not None
        name = job["metadata"]["name"]
        ended = job_ended(job)
        if request.method == "GET" and not rest:
            return web.json_response(job_details(job))
        if request.method == "GET" and rest == "/logs":
            if request.headers.get("Upgrade", "").lower() != "websocket":
                return web.json_response({"logs": await self.nrp_log(name)})
        if request.method == "GET" and rest == "/logs/tail":
            return await self.nrp_tail(request, name)
        if request.method == "POST" and rest == "/stop":
            if not ended:
                await self.delete_nrp(name)
            return web.json_response({"stopped": not ended})
        if request.method == "DELETE" and not rest:
            if not ended:
                text = "The job has not ended: stop it first"
                return web.Response(status=400, reason=text, text=text)
            await self.delete_nrp(name)
            return web.json_response({"deleted": True})
        return web.Response(status=404, text="Not found")

    async def nrp_submitter(self, name: str) -> str | None:
        """The pod of the job's submitter, which KubeRay names after it, once it runs."""
        assert self.nrp is not None
        selector = urllib.parse.quote(f"job-name={name}")
        status, listing = await self.nrp.call(
            "GET", f"{self.nrp.path('pods')}?labelSelector={selector}"
        )
        if status != 200:
            return None
        for pod in listing.get("items", []):
            if (pod.get("status") or {}).get("phase") in (
                "Running",
                "Succeeded",
                "Failed",
            ):
                return str(pod["metadata"]["name"])
        return None

    async def nrp_log(self, name: str) -> str:
        """What the job printed, as its submitter followed it."""
        assert self.nrp is not None
        pod = await self.nrp_submitter(name)
        if pod is None:
            return ""
        status, text = await self.nrp.call("GET", f"{self.nrp.path('pods')}/{pod}/log")
        return text if status == 200 and isinstance(text, str) else ""

    async def nrp_tail(self, request: web.Request, name: str) -> web.StreamResponse:
        """The job's log as its submitter prints it, until the job ends."""
        assert self.nrp is not None
        response = web.WebSocketResponse()
        await response.prepare(request)
        deadline = time.monotonic() + NRP_START_TIMEOUT_S
        pod = await self.nrp_submitter(name)
        while pod is None and time.monotonic() < deadline:
            await asyncio.sleep(START_POLL_S)
            pod = await self.nrp_submitter(name)
        if pod is not None:
            with contextlib.suppress(ConnectionResetError):
                async for line in self.nrp.lines(
                    f"{self.nrp.path('pods')}/{pod}/log?follow=true"
                ):
                    await response.send_str(line)
        await response.close()
        return response

    async def delete_nrp(self, name: str) -> None:
        """Remove a job's RayJob on NRP, and its code."""
        assert self.nrp is not None and self.bucket is not None
        await self.nrp.call(
            "DELETE",
            f"{self.nrp.path('rayjobs')}/{name}",
            {"propagationPolicy": "Background"},
        )
        await self.bucket.delete_prefix(nrp_jobs.code_prefix(name))
        self.nrp_cleaned.discard(name)

    async def reap_nrp(self) -> None:
        """Delete the code of a job on NRP once it ends, and its record once kept long
        enough; remove a job whose cluster did not start in time, and code no job took."""
        if self.nrp is None or self.bucket is None:
            return
        try:
            jobs = await self.nrp_listing()
        except (Refused, ClientError, asyncio.TimeoutError) as e:
            log.warning("listing Ray jobs on NRP: %r", e)
            return
        now = time.time()
        for job in jobs:
            name = job["metadata"]["name"]
            state = job.get("status") or {}
            try:
                created = datetime.fromisoformat(
                    job["metadata"]["creationTimestamp"]
                ).timestamp()
                if job_ended(job):
                    if name not in self.nrp_cleaned:
                        await self.bucket.delete_prefix(nrp_jobs.code_prefix(name))
                        self.nrp_cleaned.add(name)
                    ended = epoch_ms(state.get("endTime"))
                    if ended is not None and now - ended / 1000 >= JOB_RECORD_S:
                        await self.delete_nrp(name)
                        log.info("deleted the record of %s on NRP", name)
                elif state.get("jobDeploymentStatus") in (
                    None,
                    "",
                    "Initializing",
                ) and (now - created >= NRP_START_TIMEOUT_S):
                    await self.delete_nrp(name)
                    log.info(
                        "deleted %s on NRP: its cluster did not start in time", name
                    )
            except Exception:
                log.exception("checking %s on NRP", name)
        taken = {j["metadata"]["name"] for j in jobs} | {
            name for name, _ in self.nrp_waiting.values()
        }
        for name, written in (await self.bucket.jobs()).items():
            if name not in taken and now - written >= 2 * START_TIMEOUT_S:
                await self.bucket.delete_prefix(nrp_jobs.code_prefix(name))
                log.info("deleted the code of %s, which no job took", name)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """An HTTP call to a dashboard, the Jobs API's among them: a submission and
        its uploads reach a RayJob of its own, a call about a job that job's
        cluster or its record, and any other call the caller's own cluster."""
        try:
            user, shape = await self.caller(
                {key.lower(): value for key, value in request.headers.items()}
            )
            if request.path == "/api/version":
                # Answered here, so that a client asking after its jobs starts no cluster: the oldest Ray a cluster runs passes every check the client makes.
                return web.json_response(
                    {"ray_version": load_template()["spec"]["rayVersion"]}
                )
            body = None
            about = JOB_PATH.fullmatch(request.path)
            if submits(request) and shape.site == "nrp":
                return await self.nrp_submission(user, shape, request)
            if submits(request):
                head, keyed, body = await self.submission(user, shape, request)
            elif about:
                found = await self.job(user, about[1])
                if found is None:
                    on_nrp = await self.nrp_job(user, about[1])
                    if on_nrp is not None:
                        return await self.nrp_recorded(request, on_nrp, about[2] or "")
                    # A job submitted to the user's own cluster.
                    head = keyed = await self.running(user)
                else:
                    job, cluster = found
                    if cluster is None or request.method == "DELETE":
                        return await self.recorded(job, request.method, about[2] or "")
                    head, keyed = cluster, job["metadata"]["name"]
            elif request.method == "GET" and request.path == "/api/jobs/":
                listed = await self.jobs(user) + await self.nrp_jobs(user)
                return web.json_response([job_details(j) for j in listed])
            else:
                head = keyed = await self.running(user)
            url = URL(head_url(head) + request.rel_url.raw_path_qs, encoded=True)
            headers = passed_on(request.headers)
            headers += self.cluster_auth(keyed).items()
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return await self.relay_logs(request, head, url, headers)
            return await self.relay_http(request, head, url, headers, body)
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
        body: bytes | None = None,
    ) -> web.StreamResponse:
        """The call's body, or `body` in its place, and the head's answer, passed on as they come."""
        if body is None and request.body_exists:
            data: Any = request.content
        else:
            data = body
        try:
            upstream = await self.http.request(
                request.method,
                url,
                headers=headers,
                data=data,
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
        workload = token_account(token)
        if workload is not None:
            return User(
                name=workload.name,
                account=workload.name,
                uid=workload.uid,
                gid=workload.gid,
                workload=True,
            )
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        cached = self._users.get(key)
        if cached and cached[0] > now:
            refuse_account_name(cached[1].name)
            return cached[1]
        kind, name = await self.whoami(token)
        if kind != "user":
            log.warning("Hub %s refused: reason=not-a-user", kind)
            raise Refused(
                grpc.StatusCode.PERMISSION_DENIED,
                f"A Ray cluster is for a JupyterHub user, not a {kind}.",
            )
        if ":" in name:
            log.warning("Hub user refused: reason=colon")
            raise Refused(
                grpc.StatusCode.PERMISSION_DENIED,
                f"{name} is no name a Ray cluster runs for.",
            )
        refuse_account_name(name)
        account = name
        pod = session_pod(await self.hub_user(name))
        if name.endswith(EXTERNAL_SUFFIXES):
            if pod is None:
                raise Refused(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "Start your AF session first: a Ray cluster runs as the user of a running session.",
                )
            # Numbered after the hub id, which the pod name of the user's session carries.
            account = pooled_account(int(pod[1]))
        uid, gid = await asyncio.to_thread(ldap_ids, account)
        user = User(
            name=name,
            account=account,
            uid=uid,
            gid=gid,
            session_gpu=pod is not None and await self.session_holds_gpu(pod[0]),
        )
        self._users = {k: v for k, v in self._users.items() if v[0] > now}
        self._users[key] = (now + USER_CACHE_S, user)
        return user

    async def whoami(self, token: str) -> tuple[str, str]:
        """The kind and the name of the token's owner."""
        async with self.http.get(
            f"{HUB_API}/user", headers={"Authorization": f"token {token}"}
        ) as r:
            if r.status in (401, 403):
                log.warning("token refused: reason=unknown-token")
                raise Refused(
                    grpc.StatusCode.UNAUTHENTICATED,
                    "No workload account has RAY_AUTH_TOKEN, and JupyterHub did not accept it: in a session, it must be the session's JUPYTERHUB_API_TOKEN.",
                )
            if r.status != 200:
                raise Refused(
                    grpc.StatusCode.UNAVAILABLE, f"JupyterHub answered HTTP {r.status}."
                )
            model: dict[str, Any] = await r.json()
        return str(model.get("kind")), str(model["name"])

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
        headers = {"Authorization": f"Bearer {token}"}
        if method == "PATCH":
            headers["Content-Type"] = "application/merge-patch+json"
        async with self.kube_http.request(
            method,
            KUBE_API + path,
            json=None if method == "PATCH" else body,
            data=json.dumps(body) if method == "PATCH" else None,
            headers=headers,
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
        if cluster is not None and not cluster["metadata"].get("deletionTimestamp"):
            refuse_others(cluster, user)
        # Only a call the cluster's owner makes keeps it from being reaped.
        self.last_used[user.cluster] = time.monotonic()
        if cluster is not None and not runs(cluster, shape):
            if not cluster["metadata"].get("deletionTimestamp"):
                if await self.busy(cluster):
                    raise Refused(
                        grpc.StatusCode.FAILED_PRECONDITION,
                        "Your Ray cluster runs other settings, and tasks or jobs: wait for them, or stop them.",
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
                "POST",
                RAYCLUSTERS,
                build_cluster(cluster_template(user), user, shape, mix),
            )
            if status == 201:
                log.info("created %s", user.cluster)
                self.never_started.pop(user.cluster, None)
                cluster = created
            elif status == 409:
                # Another call created it first.
                cluster = await self.cluster(user.cluster)
                if cluster is not None:
                    refuse_others(cluster, user)
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
            user.cluster,
            "RayCluster",
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
        self,
        user: User,
        shape: Shape,
        replaced: dict[str, int] | None = None,
        name: str | None = None,
    ) -> dict[str, int]:
        """The workers a cluster gets of each GPU resource its shape allows, the
        first of FLAVORS first: as many of those it asks for as have free GPUs,
        or one whose GPU a preemptible worker holds. Refuse one that gets fewer
        than it starts with; `replaced` are the GPUs its predecessor gives back.
        `name` is a RayJob's, whose cluster it is; the user's own cluster's by default."""
        name = name or user.cluster
        wanted, least = int(shape.workers), int(shape.least)
        per_worker = int(shape.per_worker)
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
            # A RayJob's cluster under the name of the RayJob it was granted to.
            others = {
                (c["metadata"].get("labels") or {}).get(
                    "ray.io/originated-from-cr-name", c["metadata"]["name"]
                ): held_gpus(c)
                for c in listing.get("items", [])
            }
            others.pop(name, None)
            granted = {c: gpus for c, (gpus, _) in self.grants.items() if c != name}
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
                mix[resource] = max(
                    min(available // per_worker, wanted - sum(mix.values())), 0
                )
            available = sum(mix.values())
            if per_worker == 1 and not available and evicting:
                # One worker, and no more, may evict a preemptible one.
                mix[evicting] = available = 1
            if least > available:
                log.info(
                    "refused %s %d workers of %d GPUs: %d available, %s free",
                    name,
                    least,
                    per_worker,
                    available,
                    free,
                )
                gpus = "a GPU" if per_worker == 1 else f"{per_worker} GPUs"
                fewer = (
                    f"ask for fewer with {WORKERS_METADATA}, for those that are free with {MOST_METADATA}, or "
                    if least > 1
                    else ""
                )
                raise Refused(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    f"Your Ray cluster asks for {least} workers with {gpus} of {shape.memory} GB or more each, and Ray clusters can take {available} more now: {fewer}try again later.",
                )
            log.info("admitted %s with %d of %d workers", name, available, wanted)
            self.grants[name] = (
                {resource: workers * per_worker for resource, workers in mix.items()},
                now + GRANT_S,
            )
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

    async def session_holds_gpu(self, pod: str) -> bool:
        """Whether a session pod holds a GPU; not, if Prometheus does not say."""
        try:
            held = await self.prometheus(session_gpus_query(pod))
        except (ClientError, asyncio.TimeoutError, LookupError, TypeError, ValueError):
            log.warning("Prometheus did not say whether %s holds a GPU", pod)
            return False
        return any(count > 0 for count in held.values())

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

    async def head_status(
        self, cluster: str, authorized: bool = True, keyed: str | None = None
    ) -> int | None:
        """What the head answers, with the token of `keyed`, its RayJob, or of the cluster itself."""
        try:
            async with self.http.get(
                head_url(cluster) + "/api/version",
                headers=self.cluster_auth(keyed or cluster) if authorized else {},
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
            if (cluster["metadata"].get("labels") or {}).get(FROM_CRD) == "RayJob":
                continue
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

    async def reap_jobs(self) -> None:
        """Remove a RayJob no job was submitted to in time, and a job's record once
        kept long enough; stop a job whose workers did not all start in time."""
        status, listing = await self.kube(
            "GET",
            f"{RAYJOBS}?labelSelector=app.kubernetes.io%2Fmanaged-by%3D{MANAGED_BY}",
        )
        if status != 200:
            log.warning("listing Ray jobs: HTTP %s", status)
            return
        now = time.time()
        waited_for = {w.name for w in self.waiting.values()}
        for job in listing.get("items", []):
            metadata = job["metadata"]
            name = metadata["name"]
            state = job.get("status") or {}
            try:
                created = datetime.fromisoformat(
                    metadata["creationTimestamp"]
                ).timestamp()
                if metadata.get("deletionTimestamp"):
                    continue
                if not state.get("jobStatus"):
                    if name not in waited_for and now - created >= 2 * START_TIMEOUT_S:
                        await self.kube(
                            "DELETE",
                            f"{RAYJOBS}/{name}",
                            {"propagationPolicy": "Background"},
                        )
                        log.info("deleted %s: no job was submitted to it", name)
                elif job_ended(job):
                    ended = epoch_ms(state.get("endTime"))
                    if ended is not None and now - ended / 1000 >= JOB_RECORD_S:
                        await self.kube(
                            "DELETE",
                            f"{RAYJOBS}/{name}",
                            {"propagationPolicy": "Background"},
                        )
                        log.info("deleted the record of %s", name)
                elif head := state.get("rayClusterName"):
                    cluster = await self.cluster(head)
                    if cluster is not None and unprovisioned(cluster, now):
                        await self.stop_unprovisioned(job, head)
            except Exception:
                log.exception("checking %s", name)

    async def stop_unprovisioned(self, job: dict[str, Any], head: str) -> None:
        """Stop the job, say why on its RayJob, and free the GPUs its cluster holds."""
        name = job["metadata"]["name"]
        submission = job["spec"]["jobId"]
        with contextlib.suppress(ClientError, asyncio.TimeoutError):
            async with self.http.post(
                f"{head_url(head)}/api/jobs/{submission}/stop",
                headers=self.cluster_auth(name),
                timeout=ClientTimeout(total=10),
            ):
                pass
        await self.kube(
            "PATCH",
            f"{RAYJOBS}/{name}",
            {"metadata": {"annotations": {STOPPED_ANNOTATION: STOPPED_UNPROVISIONED}}},
        )
        await self.delete(head)
        log.info("stopped %s: its workers did not all start", name)

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
        if self.nrp is not None:
            await self.nrp.close()


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
        try:
            await gateway.reap_jobs()
        except Exception:
            log.exception("reaping Ray jobs")
        try:
            await gateway.reap_nrp()
        except Exception:
            log.exception("reaping Ray jobs on NRP")


def dashboard_app(gateway: Gateway) -> web.Application:
    async def nrp_gpus(request: web.Request) -> web.Response:
        """The GPUs free on NRP for Ray workers, by region: public, so asked by no user."""
        try:
            regions = await free_gpus(gateway.http, NRP_PROMETHEUS_URL, NRP_WORKER)
        except (ClientError, asyncio.TimeoutError, KeyError, ValueError) as error:
            log.warning("asking NRP's Prometheus: %r", error)
            return web.Response(status=503, text="NRP's Prometheus did not answer")
        return web.json_response(as_dict(regions))

    app = web.Application()
    app.router.add_get("/af/nrp-gpus", nrp_gpus)
    app.router.add_route("*", "/{path:.*}", gateway.handle)
    return app


async def serve() -> None:
    kube_tls = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    async with (
        # Unlimited: a relayed log tail holds its connection for as long as its job runs.
        ClientSession(connector=TCPConnector(limit=0)) as http,
        ClientSession(connector=TCPConnector(ssl=kube_tls)) as kube,
    ):
        nrp = bucket = None
        if NRP_KUBECONFIG.exists() and nrp_jobs.bucket_name():
            nrp = nrp_jobs.NrpApi(NRP_KUBECONFIG)
            bucket = nrp_jobs.Bucket(nrp_jobs.bucket_name())
        gateway = Gateway(http, kube, SERVICE_TOKEN_FILE, nrp, bucket)
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
