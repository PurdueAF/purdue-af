"""The GPUs free on NRP Nautilus for Ray workers of ours, by region, from NRP's
public Prometheus: those of nodes that are Ready, not cordoned and carry no
NoSchedule or NoExecute taint, less what the pods bound to them request, and
how many workers of a shape fit beside them; and the nodes a job's workers
may run on, of one region and one GPU model. An upper bound: NRP's scheduler
also weighs affinities and policies that Prometheus does not show.

    python nrp_gpus.py    # beside config.yaml and rayjob-nrp.yaml
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from aiohttp import ClientSession, ClientTimeout

GPU = "nvidia_com_gpu"
RESOURCES = f'resource=~"{GPU}|cpu|memory"'
GPU_NODES = f'on(node) kube_node_status_allocatable{{resource="{GPU}"}} > 0'
ALLOCATABLE = (
    f"sum by (node, resource) (kube_node_status_allocatable{{{RESOURCES}}})"
    f" and {GPU_NODES}"
)
# Pods count from when they are bound, as the scheduler counts them, until they end.
REQUESTED = (
    f"sum by (node, resource) (kube_pod_container_resource_requests{{{RESOURCES}}}"
    " * on(namespace, pod) group_left() (max by (namespace, pod)"
    ' (kube_pod_status_phase{phase=~"Pending|Running"}) == 1))'
)
UNSCHEDULABLE = (
    'count by (node) (kube_node_spec_taint{effect=~"NoSchedule|NoExecute"})'
    " or max by (node) (kube_node_spec_unschedulable) == 1"
    ' or max by (node) (kube_node_status_condition{condition="Ready", status="true"}) == 0'
)
REGIONS = "max by (node, label_topology_kubernetes_io_region) (kube_node_labels)"
# DCGM names a node's GPU model, which no kube-state-metrics label does.
MODELS = "count by (Hostname, modelName) (max_over_time(DCGM_FI_DEV_GPU_UTIL[1h]))"
TIMEOUT = ClientTimeout(total=60)


@dataclass(frozen=True)
class Worker:
    """The requests of one Ray worker pod."""

    gpus: int
    cpus: float
    memory_gb: float


@dataclass(frozen=True)
class Node:
    """A node with room for workers: its region, its GPU model, and how many fit."""

    name: str
    region: str
    model: str
    workers: int


@dataclass
class Region:
    gpus: int = 0
    free_gpus: int = 0
    workers: int = 0


def free_by_node(
    allocatable: list[dict[str, Any]],
    requested: list[dict[str, Any]],
    unschedulable: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]], set[str]]:
    """What each GPU node has and what its pods hold, of GPUs, CPUs and memory, and the nodes that take no pods."""
    held: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    free: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for sample in allocatable:
        free[sample["metric"]["node"]][sample["metric"]["resource"]] += float(
            sample["value"][1]
        )
    # A pod not yet bound has no node, and holds nothing.
    for sample in (s for s in requested if "node" in s["metric"]):
        held[sample["metric"]["node"]][sample["metric"]["resource"]] += float(
            sample["value"][1]
        )
    return free, held, {s["metric"]["node"] for s in unschedulable}


def fitting(has: dict[str, float], held: dict[str, float], worker: Worker) -> int:
    """How many workers fit beside what a node's pods hold."""
    gpus = int(has[GPU] - held[GPU])
    cpus = has["cpu"] - held["cpu"]
    memory_gb = (has["memory"] - held["memory"]) / 2**30
    return max(
        min(
            gpus // worker.gpus,
            int(cpus // worker.cpus),
            int(memory_gb // worker.memory_gb),
        ),
        0,
    )


def regions_of(regions: list[dict[str, Any]]) -> dict[str, str]:
    return {
        s["metric"]["node"]: s["metric"].get("label_topology_kubernetes_io_region")
        or "unlabeled"
        for s in regions
    }


def free_by_region(
    allocatable: list[dict[str, Any]],
    requested: list[dict[str, Any]],
    unschedulable: list[dict[str, Any]],
    regions: list[dict[str, Any]],
    worker: Worker,
) -> dict[str, Region]:
    """Prometheus's results of the four queries, summed by region."""
    free, held, closed = free_by_node(allocatable, requested, unschedulable)
    region_of = regions_of(regions)
    out: dict[str, Region] = defaultdict(Region)
    for node, has in free.items():
        region = out[region_of.get(node, "unlabeled")]
        region.gpus += int(has[GPU])
        if node in closed:
            continue
        region.free_gpus += max(int(has[GPU] - held[node][GPU]), 0)
        region.workers += fitting(has, held[node], worker)
    return dict(sorted(out.items()))


def open_nodes(
    allocatable: list[dict[str, Any]],
    requested: list[dict[str, Any]],
    unschedulable: list[dict[str, Any]],
    regions: list[dict[str, Any]],
    models: list[dict[str, Any]],
    worker: Worker,
) -> list[Node]:
    """The nodes with room for a worker, of a known region and GPU model."""
    free, held, closed = free_by_node(allocatable, requested, unschedulable)
    region_of = regions_of(regions)
    model_of = {s["metric"]["Hostname"]: s["metric"]["modelName"] for s in models}
    out = []
    for node, has in sorted(free.items()):
        workers = 0 if node in closed else fitting(has, held[node], worker)
        if workers and node in region_of and node in model_of:
            out.append(Node(node, region_of[node], model_of[node], workers))
    return out


def most_in_one_group(nodes: list[Node]) -> int:
    """The most workers nodes of one region and GPU model have room for."""
    groups: dict[tuple[str, str], int] = defaultdict(int)
    for node in nodes:
        groups[(node.region, node.model)] += node.workers
    return max(groups.values(), default=0)


def placement(nodes: list[Node], wanted: int, least: int) -> list[Node] | None:
    """The nodes a job's workers may run on: all of the region and GPU model with room
    for the most, the roomiest first, so that the scheduler finds room on another
    when a job placed from the same reading took one; None if no such pair has room for `least`."""
    groups: dict[tuple[str, str], list[Node]] = defaultdict(list)
    for node in nodes:
        groups[(node.region, node.model)].append(node)
    if not groups:
        return None
    best = max(groups.values(), key=lambda group: sum(n.workers for n in group))
    if sum(n.workers for n in best) < least:
        return None
    return sorted(best, key=lambda n: (-n.workers, n.name))


async def query(
    http: ClientSession, prometheus: str, promql: str
) -> list[dict[str, Any]]:
    async with http.get(
        f"{prometheus}/api/v1/query", params={"query": promql}, timeout=TIMEOUT
    ) as response:
        response.raise_for_status()
        return list((await response.json())["data"]["result"])


async def free_gpus(
    http: ClientSession, prometheus: str, worker: Worker
) -> dict[str, Region]:
    allocatable, requested, unschedulable, regions = await asyncio.gather(
        *(
            query(http, prometheus, promql)
            for promql in (ALLOCATABLE, REQUESTED, UNSCHEDULABLE, REGIONS)
        )
    )
    return free_by_region(allocatable, requested, unschedulable, regions, worker)


async def nodes(http: ClientSession, prometheus: str, worker: Worker) -> list[Node]:
    allocatable, requested, unschedulable, regions, models = await asyncio.gather(
        *(
            query(http, prometheus, promql)
            for promql in (ALLOCATABLE, REQUESTED, UNSCHEDULABLE, REGIONS, MODELS)
        )
    )
    return open_nodes(allocatable, requested, unschedulable, regions, models, worker)


def worker_of(template: dict[str, Any]) -> Worker:
    """The requests of the template's worker, a RayJob's or a RayCluster's spec."""
    spec = template["spec"].get("rayClusterSpec", template["spec"])
    (group,) = spec["workerGroupSpecs"]
    (container,) = group["template"]["spec"]["containers"]
    requests = container["resources"]["requests"]
    memory = str(requests["memory"])
    return Worker(
        int(requests["nvidia.com/gpu"]),
        float(requests["cpu"]),
        float(memory.removesuffix("Gi")),
    )


async def main() -> None:
    here = Path(__file__).parent
    config = yaml.safe_load((here / "config.yaml").read_text())
    worker = worker_of(yaml.safe_load((here / "rayjob-nrp.yaml").read_text()))
    async with ClientSession() as http:
        regions = await free_gpus(http, config["nrpPrometheusUrl"], worker)
    print(
        f"{'region':<12}{'GPUs':>7}{'free':>7}{'workers':>9}"
        f"   (a worker: {worker.gpus} GPUs, {worker.cpus:g} CPUs, {worker.memory_gb:g} GB)"
    )
    for name, region in regions.items():
        print(f"{name:<12}{region.gpus:>7}{region.free_gpus:>7}{region.workers:>9}")
    total = Region(
        sum(r.gpus for r in regions.values()),
        sum(r.free_gpus for r in regions.values()),
        sum(r.workers for r in regions.values()),
    )
    print(f"{'total':<12}{total.gpus:>7}{total.free_gpus:>7}{total.workers:>9}")


def as_dict(regions: dict[str, Region]) -> dict[str, dict[str, int]]:
    return {name: dataclasses.asdict(region) for name, region in regions.items()}


if __name__ == "__main__":
    asyncio.run(main())
