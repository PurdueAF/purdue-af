"""Ray jobs on NRP Nautilus (apps/interlink/nrp): the RayJob a submission
becomes there, the NRP API the gateway reaches with the interLink plugin's
kubeconfig, and the S3 prefix, one per job, by which a job's code, inputs and
outputs travel.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from nrp_gpus import Node

# Where a job's code is in the bucket: <JOBS_PREFIX>/<RayJob>/<package>.
JOBS_PREFIX = "ray-nrp/jobs"
# The Jobs API's names of a submission's resources, and KubeRay's.
ENTRYPOINT_RESOURCES = {
    "entrypoint_num_cpus": "entrypointNumCpus",
    "entrypoint_num_gpus": "entrypointNumGpus",
}
PACKAGE_SCHEME = "gcs://"


class NrpApi:
    """NRP's Kubernetes API, in the namespace and with the token of a kubeconfig."""

    def __init__(self, kubeconfig: Path) -> None:
        config = yaml.safe_load(kubeconfig.read_text())
        (cluster,) = config["clusters"]
        (user,) = config["users"]
        (context,) = config["contexts"]
        self.server = str(cluster["cluster"]["server"]).rstrip("/")
        self.namespace = str(context["context"]["namespace"])
        self.token = str(user["user"]["token"])
        authority = base64.b64decode(cluster["cluster"]["certificate-authority-data"])
        self.tls = ssl.create_default_context(cadata=authority.decode())
        self.http: ClientSession | None = None

    def path(self, kind: str) -> str:
        group = "/api/v1" if kind == "pods" else "/apis/ray.io/v1"
        return f"{group}/namespaces/{self.namespace}/{kind}"

    async def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> tuple[int, Any]:
        if self.http is None:
            self.http = ClientSession(connector=TCPConnector(ssl=self.tls))
        headers = {"Authorization": f"Bearer {self.token}"}
        if method == "PATCH":
            headers["Content-Type"] = "application/merge-patch+json"
        async with self.http.request(
            method,
            self.server + path,
            json=None if method == "PATCH" else body,
            data=json.dumps(body) if method == "PATCH" else None,
            headers=headers,
            timeout=ClientTimeout(total=timeout),
        ) as r:
            if r.content_type == "application/json":
                return r.status, await r.json()
            return r.status, await r.text()

    async def lines(self, path: str) -> AsyncIterator[str]:
        """A streamed answer, a line at a time, for as long as it lasts."""
        if self.http is None:
            self.http = ClientSession(connector=TCPConnector(ssl=self.tls))
        async with self.http.get(
            self.server + path,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=ClientTimeout(total=None, sock_connect=10),
        ) as r:
            async for line in r.content:
                yield line.decode(errors="replace")

    async def close(self) -> None:
        if self.http is not None:
            await self.http.close()


def owner_key(cluster: str, owner: str) -> str:
    """Whose a job on NRP is, without their name: NRP's admins read its labels."""
    return hashlib.sha256(f"{owner}/{cluster}".encode()).hexdigest()[:32]


def code_prefix(job: str) -> str:
    return f"{JOBS_PREFIX}/{job}"


def data_uri(bucket: str, job: str, part: str) -> str:
    """Where a job's inputs or outputs are while it runs on NRP."""
    return f"s3://{bucket}/{code_prefix(job)}/{part}"


def to_s3(runtime_env: dict[str, Any], bucket: str, job: str) -> dict[str, Any]:
    """The runtime environment with each uploaded package at the job's S3 prefix."""

    def moved(uri: Any) -> Any:
        if isinstance(uri, str) and uri.startswith(PACKAGE_SCHEME):
            return (
                f"s3://{bucket}/{code_prefix(job)}/{uri.removeprefix(PACKAGE_SCHEME)}"
            )
        return uri

    out = dict(runtime_env)
    if "working_dir" in out:
        out["working_dir"] = moved(out["working_dir"])
    if isinstance(out.get("py_modules"), list):
        out["py_modules"] = [moved(m) for m in out["py_modules"]]
    return out


def build_job(
    template: dict[str, Any],
    name: str,
    owner: str,
    submission: dict[str, Any],
    bucket: str,
    workers: int,
    ttl: int,
    data: dict[str, str],
) -> dict[str, Any]:
    """The RayJob of a submission: the template's, with `workers` workers, created
    suspended until the gateway places them (placed), which KubeRay removes `ttl`
    seconds after the job ends. `data` are the job's AF_ variables, its inputs and outputs in S3."""
    job = copy.deepcopy(template)
    metadata = job["metadata"]
    metadata["name"] = name
    metadata["labels"]["purdue-af/ray-owner"] = owner
    metadata["annotations"] = {
        "purdue-af/ray-entrypoint": str(submission.get("entrypoint", ""))[:4096]
    }
    spec = job["spec"]
    spec["suspend"] = True
    spec["jobId"] = submission["submission_id"]
    spec["entrypoint"] = str(submission["entrypoint"])
    spec["ttlSecondsAfterFinished"] = ttl
    runtime_env = dict(submission.get("runtime_env") or {})
    if data:
        runtime_env["env_vars"] = {**(runtime_env.get("env_vars") or {}), **data}
    if runtime_env:
        spec["runtimeEnvYAML"] = yaml.safe_dump(to_s3(runtime_env, bucket, name))
    for asked, field in ENTRYPOINT_RESOURCES.items():
        if submission.get(asked) is not None:
            spec[field] = submission[asked]
    if submission.get("entrypoint_resources"):
        spec["entrypointResources"] = json.dumps(submission["entrypoint_resources"])
    (group,) = spec["rayClusterSpec"]["workerGroupSpecs"]
    group.update(replicas=workers, minReplicas=workers, maxReplicas=workers)
    return job


def placed(job: dict[str, Any], workers: int, nodes: list[Node]) -> dict[str, Any]:
    """The job's cluster with `workers` workers on `nodes`, and its head in their region."""
    cluster: dict[str, Any] = copy.deepcopy(job["spec"]["rayClusterSpec"])
    (group,) = cluster["workerGroupSpecs"]
    group.update(replicas=workers, minReplicas=workers, maxReplicas=workers)
    hosts = sorted(n.name for n in nodes)
    group["template"]["spec"]["affinity"] = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": hosts,
                            }
                        ]
                    }
                ]
            }
        }
    }
    cluster["headGroupSpec"]["template"]["spec"]["nodeSelector"] = {
        "topology.kubernetes.io/region": nodes[0].region
    }
    return cluster


def workers_of(job: dict[str, Any]) -> int:
    (group,) = job["spec"]["rayClusterSpec"]["workerGroupSpecs"]
    return int(group["replicas"])


def held_gpus(job: dict[str, Any]) -> int:
    """The GPUs a RayJob's workers ask for."""
    total = 0
    for group in job["spec"]["rayClusterSpec"].get("workerGroupSpecs") or []:
        for container in group["template"]["spec"]["containers"]:
            requests = (container.get("resources") or {}).get("requests") or {}
            total += int(group.get("replicas", 0)) * int(
                requests.get("nvidia.com/gpu", 0)
            )
    return total


class Bucket:
    """The NRP S3 bucket of the gateway's nrp-s3 keys; boto3 reads the endpoint from AWS_ENDPOINT_URL."""

    def __init__(self, name: str) -> None:
        import boto3

        self.name = name
        self.s3 = boto3.client("s3")

    async def put(self, key: str, body: bytes) -> None:
        await asyncio.to_thread(
            self.s3.put_object, Bucket=self.name, Key=key, Body=body
        )

    async def exists(self, key: str) -> bool:
        listed = await asyncio.to_thread(
            self.s3.list_objects_v2, Bucket=self.name, Prefix=key, MaxKeys=1
        )
        return any(o["Key"] == key for o in listed.get("Contents", []))

    async def delete_prefix(self, prefix: str) -> int:
        """Delete everything under `prefix`/; how many objects there were."""
        deleted = 0
        while True:
            listed = await asyncio.to_thread(
                self.s3.list_objects_v2, Bucket=self.name, Prefix=prefix + "/"
            )
            keys = [{"Key": o["Key"]} for o in listed.get("Contents", [])]
            if not keys:
                return deleted
            await asyncio.to_thread(
                self.s3.delete_objects, Bucket=self.name, Delete={"Objects": keys}
            )
            deleted += len(keys)

    async def jobs(self) -> dict[str, float]:
        """The jobs with code in the bucket, and when their newest object was written."""
        newest: dict[str, float] = {}
        paginator = self.s3.get_paginator("list_objects_v2")
        pages = await asyncio.to_thread(
            lambda: list(paginator.paginate(Bucket=self.name, Prefix=JOBS_PREFIX + "/"))
        )
        for page in pages:
            for o in page.get("Contents", []):
                job = o["Key"].removeprefix(JOBS_PREFIX + "/").split("/", 1)[0]
                # pyarrow marks a directory it writes into with an empty object.
                if job:
                    newest[job] = max(
                        newest.get(job, 0.0), o["LastModified"].timestamp()
                    )
        return newest


def bucket_name() -> str:
    return os.environ.get("S3_BUCKET", "")
