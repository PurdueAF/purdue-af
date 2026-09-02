"""Live GPU availability for the session profile picker.

Queries the same Prometheus / kube-state-metrics data the JupyterHub profile
form uses (apps/jupyterhub/.../extraFiles/gpu-availability.py) so counts shown
in the agentic "GPUs" question match what the Hub would show, and the Hub's
modify_pod_hook still gates the actual spawn. Fail-open: if Prometheus is
unreachable the count is unknown and no choice is hidden.
"""

import os
import time
from typing import Optional

import httpx
from errors import describe_exception, response_detail
from shared import shared_client

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-server:9090")

# k8s extended GPU resource -> kube-state-metrics resource label.
GPU_METRICS = {
    "nvidia.com/mig-1g.5gb": "nvidia_com_mig_1g_5gb",
    "nvidia.com/mig-7g.40gb": "nvidia_com_mig_7g_40gb",
    "nvidia.com/gpu": "nvidia_com_gpu",
}

# Availability = allocatable - requested on schedulable (not cordoned) tainted
# cms-af nodes; only Pending/Running pods count as using a GPU. Identical to the
# Hub's queries so the numbers agree. Covers MIG slices (A100) and whole GPUs
# (T4 via nvidia.com/gpu).
_NODE_SCOPE = (
    " * on (node) group_left() (kube_node_spec_unschedulable == bool 0)"
    ' * on (node) group_left() group by (node) (kube_node_spec_taint{value="cms-af"})'
)
_GPU_RESOURCE = 'resource=~"nvidia_com_(mig_.+|gpu)"'
_ALLOC_QUERY = (
    "sum by (resource) (kube_node_status_allocatable{"
    + _GPU_RESOURCE
    + "}"
    + _NODE_SCOPE
    + ")"
)
_USED_QUERY = (
    "sum by (resource) ("
    "kube_pod_container_resource_requests{"
    + _GPU_RESOURCE
    + "}"
    + _NODE_SCOPE
    + " * on (namespace, pod) group_left() (max by (namespace, pod) "
    '(kube_pod_status_phase{phase=~"Pending|Running"}) == bool 1))'
)

_CACHE_TTL = 30.0
_cache: tuple[float, Optional[dict[str, int]]] = (0.0, None)
# Why availability is unknown, for the tools that show it. None when known.
_last_error: Optional[str] = None


def gpu_error() -> Optional[str]:
    """Why free_gpus() last answered None, or None when it answered."""
    return _last_error


async def _prom_query(client: httpx.AsyncClient, query: str) -> dict[str, float]:
    resp = await client.get(
        f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=5.0
    )
    resp.raise_for_status()
    return {
        sample["metric"].get("resource"): float(sample["value"][1])
        for sample in resp.json()["data"]["result"]
    }


async def free_gpus() -> Optional[dict[str, int]]:
    """{k8s GPU resource: free count} on schedulable AF nodes; None if unknown."""
    global _cache, _last_error
    now = time.monotonic()
    if now < _cache[0]:
        return _cache[1]

    free: Optional[dict[str, int]] = None
    try:
        client = shared_client("prometheus")
        allocatable = await _prom_query(client, _ALLOC_QUERY)
        used = await _prom_query(client, _USED_QUERY)
        if allocatable:  # empty => kube-state-metrics missing => unknown, not zero
            free = {
                resource: max(int(allocatable.get(m, 0) - used.get(m, 0)), 0)
                for resource, m in GPU_METRICS.items()
            }
            _last_error = None
        else:
            _last_error = (
                "the monitoring system has no GPU capacity metrics "
                "(kube-state-metrics may be down)"
            )
    except httpx.HTTPStatusError as exc:
        detail = response_detail(exc.response, limit=120)
        _last_error = f"Prometheus returned HTTP {exc.response.status_code}" + (
            f" — {detail}" if detail else ""
        )
        free = None
    except httpx.HTTPError as exc:
        _last_error = f"Prometheus unreachable — {describe_exception(exc)}"
        free = None
    except (KeyError, ValueError, TypeError):
        _last_error = (
            "the monitoring system returned GPU metrics in an unexpected shape"
        )
        free = None

    _cache = (now + _CACHE_TTL, free)
    return free


def apply_availability(
    choices: dict[str, str],
    gpu_map: dict[str, str],
    free: Optional[dict[str, int]],
) -> tuple[dict[str, str], list[str]]:
    """Annotate GPU choices with live counts and drop exhausted flavors.

    ``choices`` maps key -> label; ``gpu_map`` maps key -> k8s GPU resource for
    the GPU-requesting choices; ``free`` is {resource: count} or None (unknown).
    Returns ``(labels, keys)`` preserving order — non-GPU choices are untouched,
    and when availability is unknown nothing is changed or hidden (fail-open).
    """
    labels: dict[str, str] = {}
    keys: list[str] = []
    for key, label in choices.items():
        resource = gpu_map.get(key)
        if resource is None or free is None:
            labels[key] = label
            keys.append(key)
            continue
        count = free.get(resource, 0)
        if count <= 0:
            continue  # exhausted flavor — hide the choice
        base = label.removesuffix(" - subject to availability")
        labels[key] = f"{base} — {count} available now"
        keys.append(key)
    return labels, keys
