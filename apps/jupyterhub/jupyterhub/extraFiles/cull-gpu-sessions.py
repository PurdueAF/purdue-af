"""Cull sessions that hold a full A100 GPU after 24 hours of inactivity.

Runs as a JupyterHub *managed service* inside the hub pod (see
hub.services.gpu-culler and hub.loadRoles.gpu-culler in values.yaml), the
same mechanism z2jh uses for its own idle culler. The global culler stops
any session after `cull.timeout` (14 days) of inactivity; full 40GB GPUs
are scarce, so sessions holding one get a much shorter leash.

Full-GPU pods are found through the Kubernetes API (the service runs with
the hub's service account, which kubespawner already grants pod list/get).
Idleness comes from the hub REST API — the same last_activity signal
(proxy traffic + notebook activity reports) the global culler uses. The
pod's hub.jupyter.org/username and /servername annotations (set by
kubespawner) link the two.
"""

import argparse
import asyncio
import datetime
import json
import os
from urllib.parse import quote

from tornado.httpclient import AsyncHTTPClient, HTTPRequest

GPU_RESOURCE = "nvidia.com/mig-7g.40gb"
# Only pods spawned by our hub carry this label (singleuser.extraLabels).
POD_SELECTOR = "username_unescaped"


def pod_holds_full_gpu(pod):
    """True if any container in the pod requests at least one full GPU."""
    return any(
        int(
            ((container.resources and container.resources.limits) or {}).get(
                GPU_RESOURCE, 0
            )
        )
        > 0
        for container in pod.spec.containers
    )


def pod_server(pod):
    """(username, server name) a singleuser pod belongs to, from kubespawner
    annotations; username is None for pods that are not hub-spawned."""
    annotations = pod.metadata.annotations or {}
    username = annotations.get("hub.jupyter.org/username")
    servername = annotations.get("hub.jupyter.org/servername", "")
    return username, servername


async def full_gpu_servers(namespace):
    """[(username, server name)] of running pods that hold a full GPU."""
    # Imported lazily so the unit tests don't need the kubernetes client.
    from kubernetes_asyncio import client, config

    config.load_incluster_config()
    async with client.ApiClient() as api:
        pods = await client.CoreV1Api(api).list_namespaced_pod(
            namespace, label_selector=POD_SELECTOR
        )
    servers = []
    for pod in pods.items:
        if pod.status.phase != "Running" or not pod_holds_full_gpu(pod):
            continue
        username, servername = pod_server(pod)
        if username is not None:
            servers.append((username, servername))
    return servers


async def hub_api(method, path):
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


def idle_seconds(server, now):
    """Seconds since the server's last activity (spawn time if none yet)."""
    timestamp = server.get("last_activity") or server.get("started")
    if not timestamp:
        return 0
    last = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (now - last).total_seconds()


async def cull_once(namespace, timeout):
    now = datetime.datetime.now(datetime.timezone.utc)
    for username, servername in await full_gpu_servers(namespace):
        user = await hub_api("GET", f"/users/{quote(username, safe='')}")
        server = (user.get("servers") or {}).get(servername)
        if server is None or server.get("pending"):
            continue
        idle = idle_seconds(server, now)
        if idle < timeout:
            continue
        print(
            f"[gpu-culler] stopping server {username}/{servername or 'default'}: "
            f"holds a full GPU and idle for {idle / 3600:.1f}h"
        )
        path = f"/users/{quote(username, safe='')}"
        path += f"/servers/{quote(servername, safe='')}" if servername else "/server"
        await hub_api("DELETE", path)


async def main(namespace, timeout, every):
    print(
        f"[gpu-culler] culling {GPU_RESOURCE} sessions idle > {timeout}s "
        f"in namespace {namespace}, checking every {every}s"
    )
    while True:
        try:
            await cull_once(namespace, timeout)
        except Exception as exc:
            print(f"[gpu-culler] cull pass failed: {exc}")
        await asyncio.sleep(every)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=int, default=86400, help="idle seconds before culling"
    )
    parser.add_argument(
        "--every", type=int, default=600, help="seconds between cull passes"
    )
    parser.add_argument("--namespace", default=os.environ.get("POD_NAMESPACE", "cms"))
    args = parser.parse_args()
    asyncio.run(main(args.namespace, args.timeout, args.every))
