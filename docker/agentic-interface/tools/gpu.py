"""Live GPU availability for the session profile picker.

Counts are Prometheus' allocatable minus requested (gpu_queries.py, the queries
the JupyterHub profile form runs), without the Hub's in-flight reservations; its
modify_pod_hook still gates the actual spawn. Fail-open: if the count is unknown
no choice is hidden.
"""

import asyncio
from typing import Optional

import httpx
from cachetools import TTLCache
from config import PROMETHEUS_NAME, PROMETHEUS_URL
from gpu_queries import ALLOC_QUERY as _ALLOC_QUERY
from gpu_queries import GPU_METRICS
from gpu_queries import USED_QUERY as _USED_QUERY
from shared import prom_query, prom_vector, shared_client

_CACHE_TTL = 30.0
# {resource: count} or None (unknown); a broken Prometheus is not re-asked either.
_cache: TTLCache[str, Optional[dict[str, int]]] = TTLCache(1, _CACHE_TTL)
# Why availability is unknown, for the tools that show it. None when known.
_last_error: Optional[str] = None


def gpu_error() -> Optional[str]:
    """Why free_gpus() last answered None, or None when it answered."""
    return _last_error


async def _resource_counts(
    client: httpx.AsyncClient, query: str
) -> tuple[dict[str, float], Optional[str]]:
    """{metric resource label: value} and the problem, as prom_query reports it.

    A row prom_vector skips is a problem: its flavor would read as none free.
    """
    rows, problem = await prom_query(client, PROMETHEUS_URL, query, timeout=5.0)
    samples = prom_vector(rows)
    if len(samples) < len(rows):
        problem = problem or "returned GPU metrics in an unexpected shape"
    return {labels.get("resource", ""): value for labels, value in samples}, problem


async def free_gpus() -> Optional[dict[str, int]]:
    """{k8s GPU resource: free count} on schedulable AF nodes; None if unknown."""
    global _last_error
    if "free" in _cache:
        return _cache["free"]

    client = shared_client("prometheus")
    (allocatable, problem), (used, used_problem) = await asyncio.gather(
        _resource_counts(client, _ALLOC_QUERY),
        _resource_counts(client, _USED_QUERY),
    )
    problem = problem or used_problem

    free: Optional[dict[str, int]] = None
    if problem:
        _last_error = f"{PROMETHEUS_NAME} {problem}"
    elif not allocatable:  # kube-state-metrics missing => unknown, not zero
        _last_error = (
            f"{PROMETHEUS_NAME} has no GPU capacity metrics "
            "(kube-state-metrics may be down)"
        )
    else:
        free = {
            resource: max(int(allocatable.get(m, 0) - used.get(m, 0)), 0)
            for resource, m in GPU_METRICS.items()
        }
        _last_error = None

    _cache["free"] = free
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
