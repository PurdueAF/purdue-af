"""Live GPU availability for the session profile picker.

Runs the very queries the JupyterHub profile form runs (gpu_queries.py, shared
with apps/jupyterhub/.../extraFiles/gpu-availability.py and copied into this
image) so counts shown in the agentic "GPUs" question match what the Hub
would show, and the Hub's modify_pod_hook still gates the actual spawn.
Fail-open: if Prometheus is unreachable the count is unknown and no choice
is hidden.
"""

from typing import Optional

import httpx
from cachetools import TTLCache
from config import PROMETHEUS_URL
from errors import describe_exception, response_detail
from gpu_queries import ALLOC_QUERY as _ALLOC_QUERY
from gpu_queries import GPU_METRICS
from gpu_queries import USED_QUERY as _USED_QUERY
from shared import shared_client

_CACHE_TTL = 30.0
# "free" → {resource: count} or None (unknown); both outcomes are cached so
# a broken Prometheus is not re-asked on every profile question.
_cache: TTLCache[str, Optional[dict[str, int]]] = TTLCache(1, _CACHE_TTL)
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
    global _last_error
    if "free" in _cache:
        return _cache["free"]

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
