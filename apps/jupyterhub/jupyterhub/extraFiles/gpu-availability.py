"""Live GPU availability on the profile form + refusal of doomed GPU spawns.

z2jh execs jupyterhub_config.d snippets in the chart config's own namespace,
after c.KubeSpawner.profile_list has been populated from singleuser.profileList.
This snippet wraps that static list in a callable so that every render of the
profile selection form annotates GPU choices with the number of GPUs of that
flavor that are free right now, and registers a modify_pod_hook that refuses
to create a pod requesting a GPU flavor with none left (otherwise the pod
would sit Pending until start_timeout).

Availability = allocatable - requested on schedulable (not cordoned) cms-af
nodes, from Prometheus / kube-state-metrics — the same data the Grafana
dashboards use, so no extra hub RBAC is needed. If Prometheus is unreachable
the form keeps the plain static labels and GPU spawns are allowed (fail open).
"""

import asyncio
import copy
import json
import os
import time
from urllib.parse import urlencode

from tornado.httpclient import AsyncHTTPClient

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-server:9090")
PROMETHEUS_TIMEOUT = 5  # seconds
CACHE_TTL = 30  # seconds; form renders reuse the last Prometheus answer this long
GRANT_TTL = 120  # seconds; admitted spawns count as used until Prometheus sees them

# k8s extended resource -> kube-state-metrics resource label + human-readable
# name; an optional "note" is always appended to the choice on the profile form
GPU_FLAVORS = {
    "nvidia.com/mig-1g.5gb": {
        "metric": "nvidia_com_mig_1g_5gb",
        "label": "A100 GPU slices (5GB)",
        "note": "idle session timeout 14 days",
    },
    "nvidia.com/mig-7g.40gb": {
        "metric": "nvidia_com_mig_7g_40gb",
        "label": "full A100 GPUs (40GB)",
        # keep in sync with the gpu-culler --timeout in values.yaml
        "note": "idle session timeout 24h",
    },
}

# Scope both sides of the subtraction to schedulable AF nodes: tainted cms-af
# and not cordoned ("== bool" / "group by" turn the join vectors into 0/1
# weights). Pods on a cordoned node keep running but neither they nor the
# node's capacity count towards what a new pod can be scheduled on.
_NODE_SCOPE = (
    " * on (node) group_left() (kube_node_spec_unschedulable == bool 0)"
    ' * on (node) group_left() group by (node) (kube_node_spec_taint{value="cms-af"})'
)
_ALLOC_QUERY = (
    "sum by (resource) ("
    'kube_node_status_allocatable{resource=~"nvidia_com_mig_.+"}' + _NODE_SCOPE + ")"
)
# Completed/failed pods keep their kube-state-metrics request series, so only
# count pods that are currently Pending or Running.
_USED_QUERY = (
    "sum by (resource) ("
    'kube_pod_container_resource_requests{resource=~"nvidia_com_mig_.+"}'
    + _NODE_SCOPE
    + " * on (namespace, pod) group_left() (max by (namespace, pod) "
    '(kube_pod_status_phase{phase=~"Pending|Running"}) == bool 1)'
    ")"
)


async def _prom_query(query):
    """Instant PromQL query -> {resource label: value}. Raises on failure."""
    url = f"{PROMETHEUS_URL}/api/v1/query?" + urlencode({"query": query})
    response = await AsyncHTTPClient().fetch(
        url, connect_timeout=PROMETHEUS_TIMEOUT, request_timeout=PROMETHEUS_TIMEOUT
    )
    payload = json.loads(response.body)
    return {
        sample["metric"].get("resource"): float(sample["value"][1])
        for sample in payload["data"]["result"]
    }


async def get_free_gpus():
    """{k8s resource: free count} on schedulable AF nodes; None when unknown."""
    try:
        allocatable, used = await asyncio.gather(
            _prom_query(_ALLOC_QUERY), _prom_query(_USED_QUERY)
        )
    except Exception as exc:
        print(f"[gpu-availability] Prometheus query failed: {exc}")
        return None
    if not allocatable:
        # kube-state-metrics data missing — treat as unknown, not as zero
        print("[gpu-availability] no allocatable GPU metrics in Prometheus")
        return None
    return {
        resource: max(
            int(allocatable.get(flavor["metric"], 0) - used.get(flavor["metric"], 0)),
            0,
        )
        for resource, flavor in GPU_FLAVORS.items()
    }


# kube-state-metrics only sees a newly admitted pod after the next scrape, so
# remember our own recent admissions and subtract them from the availability.
_recent_grants = []  # [(monotonic timestamp, k8s resource name)]
_cache = {"expires": 0.0, "free": None}


def _grants_in_flight(resource):
    now = time.monotonic()
    _recent_grants[:] = [(t, r) for (t, r) in _recent_grants if now - t < GRANT_TTL]
    return sum(1 for (_, r) in _recent_grants if r == resource)


async def free_gpus(use_cache=True):
    """Cached availability minus spawns admitted in the last GRANT_TTL seconds."""
    now = time.monotonic()
    if not use_cache or _cache["free"] is None or now >= _cache["expires"]:
        _cache["free"] = await get_free_gpus()
        _cache["expires"] = now + CACHE_TTL
    if _cache["free"] is None:
        return None
    return {
        resource: max(count - _grants_in_flight(resource), 0)
        for resource, count in _cache["free"].items()
    }


def _gpu_requests_in(kubespawner_override):
    """GPU resources (with amount > 0) requested by a kubespawner override."""
    limits = (kubespawner_override or {}).get("extra_resource_limits") or {}
    return {
        resource: int(amount)
        for resource, amount in limits.items()
        if resource in GPU_FLAVORS and int(amount) > 0
    }


def _annotate_gpu_choices(profiles, free):
    """Append live availability and flavor notes to every GPU choice.

    The flavor note (e.g. the 24h inactivity limit) is appended even when
    availability is unknown (free is None); the live count replaces the
    static "subject to availability" hedge only when it is actually known.
    """
    for profile in profiles:
        for option in (profile.get("profile_options") or {}).values():
            for choice in (option.get("choices") or {}).values():
                for resource in _gpu_requests_in(choice.get("kubespawner_override")):
                    parts = []
                    count = None if free is None else free.get(resource)
                    if count is not None:
                        parts.append(
                            f"{count} available now"
                            if count
                            else "none available right now"
                        )
                    note = GPU_FLAVORS[resource].get("note")
                    if note:
                        parts.append(note)
                    if not parts:
                        continue
                    name = choice["display_name"]
                    if count is not None:
                        name = name.removesuffix(" - subject to availability")
                    choice["display_name"] = f"{name} — {', '.join(parts)}"
    return profiles


_static_profile_list = c.KubeSpawner.profile_list


async def profile_list_with_gpu_counts(spawner):
    profiles = copy.deepcopy(_static_profile_list)
    free = await free_gpus()
    return _annotate_gpu_choices(profiles, free)


class GPUsUnavailableError(Exception):
    """Aborts a spawn that requests an exhausted GPU flavor."""


def hide_gpus_from_pod(pod):
    """Inject NVIDIA_VISIBLE_DEVICES=void into every container of a 0-GPU pod.

    The 0.13.x AF image is built on the NVIDIA CUDA base, which bakes
    NVIDIA_VISIBLE_DEVICES=all — and the cluster runtime honors it, so a
    0-GPU session would see every GPU on its node. The guard lives HERE, in
    the pod spec, rather than as an image ENV override: pod-spec env always
    beats image env, GPU sessions are never touched (the device plugin's
    injection for the allocated device works exactly as it does for the
    0.12.x image), and the fix applies to any image the hub spawns.
    """
    try:
        from kubernetes_asyncio.client.models import V1EnvVar
    except ImportError:  # unit tests exercise plain namespaces
        from types import SimpleNamespace as V1EnvVar
    for container in pod.spec.containers:
        env = getattr(container, "env", None) or []
        if any(getattr(e, "name", None) == "NVIDIA_VISIBLE_DEVICES" for e in env):
            continue  # explicitly configured elsewhere — leave it alone
        env.append(V1EnvVar(name="NVIDIA_VISIBLE_DEVICES", value="void"))
        container.env = env


async def refuse_gpu_spawn_if_unavailable(spawner, pod):
    """modify_pod_hook: re-check availability at spawn time and refuse if 0.

    The form annotation above is only cosmetic (and may be stale by the time
    the user clicks Start); this is the actual gate, and it also covers spawns
    that bypass the form (e.g. the REST API / agentic interface). It also
    hides node GPUs from sessions that did not request any (see
    hide_gpus_from_pod).
    """
    requested = {}
    any_gpu = False
    for container in pod.spec.containers:
        limits = getattr(container.resources, "limits", None) or {}
        for resource, amount in limits.items():
            if resource.startswith("nvidia.com/") and int(amount) > 0:
                any_gpu = True
            if resource in GPU_FLAVORS and int(amount) > 0:
                requested[resource] = requested.get(resource, 0) + int(amount)
    if not any_gpu:
        hide_gpus_from_pod(pod)
        return pod
    if not requested:
        return pod

    free = await free_gpus(use_cache=False)
    if free is None:
        # Prometheus unreachable: don't lock users out, let the scheduler decide.
        spawner.log.warning("[gpu-availability] availability unknown, allowing spawn")
        return pod

    for resource, amount in requested.items():
        if free.get(resource, 0) < amount:
            label = GPU_FLAVORS[resource]["label"]
            raise GPUsUnavailableError(
                f"All {label} are currently in use by other sessions. "
                "Please start a session without a GPU, or try again later."
            )

    _recent_grants.extend(
        (time.monotonic(), resource)
        for resource, amount in requested.items()
        for _ in range(amount)
    )
    return pod


if isinstance(_static_profile_list, list) and _static_profile_list:
    c.KubeSpawner.profile_list = profile_list_with_gpu_counts
else:
    print("[gpu-availability] static profileList not found; labels stay static")

c.KubeSpawner.modify_pod_hook = refuse_gpu_spawn_if_unavailable
