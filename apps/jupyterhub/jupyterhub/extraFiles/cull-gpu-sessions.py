"""Stop sessions that hold a GPU once they are idle for --timeout seconds.

A JupyterHub managed service: GPU pods come from the Kubernetes API, idleness
from the hub API, and their hub.jupyter.org annotations link the two. It exits
once kubelet swaps in a new copy of this file, and the Hub starts it again.
"""

import argparse
import asyncio
import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tornado.httpclient import AsyncHTTPClient, HTTPRequest

# Only pods spawned by our hub carry this label (singleuser.extraLabels).
POD_SELECTOR = "username_unescaped"
NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
SOURCE = Path(__file__)

log = logging.getLogger("gpu-culler")


def default_namespace() -> str:
    """Namespace the hub pod runs in, from the mounted service account."""
    return NAMESPACE_FILE.read_text().strip()


def pod_holds_gpu(pod: Any) -> bool:
    """True if any container in the pod has a GPU limit above 0."""
    for container in pod.spec.containers:
        limits = (container.resources and container.resources.limits) or {}
        for resource, amount in limits.items():
            if resource.startswith("nvidia.com/") and int(amount) > 0:
                return True
    return False


def pod_server(pod: Any) -> tuple[str | None, str]:
    """(username, server name) a singleuser pod belongs to, from kubespawner
    annotations; username is None for pods that are not hub-spawned."""
    annotations = pod.metadata.annotations or {}
    username = annotations.get("hub.jupyter.org/username")
    servername = annotations.get("hub.jupyter.org/servername", "")
    return username, servername


async def gpu_servers(namespace: str) -> list[tuple[str, str]]:
    """[(username, server name)] of running pods that hold a GPU."""
    # Imported lazily so the unit tests don't need the kubernetes client.
    from kubernetes_asyncio import client, config

    config.load_incluster_config()
    async with client.ApiClient() as api:
        pods = await client.CoreV1Api(api).list_namespaced_pod(
            namespace, label_selector=POD_SELECTOR
        )
    servers = []
    for pod in pods.items:
        if pod.status.phase != "Running" or not pod_holds_gpu(pod):
            continue
        username, servername = pod_server(pod)
        if username is not None:
            servers.append((username, servername))
    return servers


async def hub_api(method: str, path: str) -> Any:
    """JupyterHub REST call using the managed-service credentials."""
    url = os.environ["JUPYTERHUB_API_URL"].rstrip("/") + path
    response = await AsyncHTTPClient().fetch(
        HTTPRequest(
            url,
            method=method,
            headers={"Authorization": f"token {os.environ['JUPYTERHUB_API_TOKEN']}"},
        )
    )
    return json.loads(response.body) if response.body else None


def idle_seconds(server: dict[str, Any], now: datetime.datetime) -> float:
    """Seconds since the server's last activity (spawn time if none yet)."""
    timestamp = server.get("last_activity") or server.get("started")
    if not timestamp:
        return 0.0
    last = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return float((now - last).total_seconds())


async def cull_once(namespace: str, timeout: float) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    for username, servername in await gpu_servers(namespace):
        # One server the hub cannot answer for must not shield the servers after it.
        try:
            await cull_server(username, servername, now, timeout)
        except Exception:
            log.exception("could not cull %s/%s", username, servername or "default")


async def cull_server(
    username: str, servername: str, now: datetime.datetime, timeout: float
) -> None:
    user = await hub_api("GET", f"/users/{quote(username, safe='')}")
    server = (user.get("servers") or {}).get(servername)
    if server is None or server.get("pending"):
        return
    idle = idle_seconds(server, now)
    if idle < timeout:
        return
    log.info(
        "stopping server %s/%s: holds a GPU and idle for %.1fh",
        username,
        servername or "default",
        idle / 3600,
    )
    path = f"/users/{quote(username, safe='')}"
    path += f"/servers/{quote(servername, safe='')}" if servername else "/server"
    await hub_api("DELETE", path)


async def main(namespace: str, timeout: float, every: float) -> None:
    log.info(
        "culling GPU sessions idle > %ss in namespace %s, checking every %ss",
        timeout,
        namespace,
        every,
    )
    running = SOURCE.read_bytes()
    while True:
        try:
            await cull_once(namespace, timeout)
        except Exception:
            log.exception("cull pass failed")
        await asyncio.sleep(every)
        if SOURCE.read_bytes() != running:
            log.info("%s changed: exiting, for the Hub to start it again", SOURCE)
            return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=int, required=True, help="idle seconds before culling"
    )
    parser.add_argument(
        "--every", type=int, required=True, help="seconds between cull passes"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="[%(name)s] %(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,  # stdout here is a block-buffered pipe
    )
    asyncio.run(main(default_namespace(), args.timeout, args.every))
