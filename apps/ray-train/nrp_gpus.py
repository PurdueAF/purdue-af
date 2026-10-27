"""The GPUs free on NRP Nautilus for Ray workers of ours, by region, from NRP's
public Prometheus: those of nodes that are Ready, not cordoned and carry no
NoSchedule or NoExecute taint, less what the pods bound to them request, and
how many workers of a shape fit beside them. An upper bound: NRP's scheduler
also weighs affinities and policies that Prometheus does not show.

    python nrp_gpus.py    # beside config.yaml
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
TIMEOUT = ClientTimeout(total=60)


@dataclass(frozen=True)
class Worker:
    """The requests of one Ray worker pod."""

    gpus: int
    cpus: float
    memory_gb: float


@dataclass
class Region:
    gpus: int = 0
    free_gpus: int = 0
    workers: int = 0


def free_by_region(
    allocatable: list[dict[str, Any]],
    requested: list[dict[str, Any]],
    unschedulable: list[dict[str, Any]],
    regions: list[dict[str, Any]],
    worker: Worker,
) -> dict[str, Region]:
    """Prometheus's results of the four queries, summed by region."""
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
    closed = {s["metric"]["node"] for s in unschedulable}
    region_of = {
        s["metric"]["node"]: s["metric"].get("label_topology_kubernetes_io_region")
        or "unlabeled"
        for s in regions
    }
    out: dict[str, Region] = defaultdict(Region)
    for node, has in free.items():
        region = out[region_of.get(node, "unlabeled")]
        region.gpus += int(has[GPU])
        if node in closed:
            continue
        gpus = int(has[GPU] - held[node][GPU])
        cpus = has["cpu"] - held[node]["cpu"]
        memory_gb = (has["memory"] - held[node]["memory"]) / 2**30
        region.free_gpus += max(gpus, 0)
        region.workers += max(
            min(
                gpus // worker.gpus,
                int(cpus // worker.cpus),
                int(memory_gb // worker.memory_gb),
            ),
            0,
        )
    return dict(sorted(out.items()))


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


def worker_of(config: dict[str, Any]) -> Worker:
    shape = config["nrpWorker"]
    return Worker(int(shape["gpus"]), float(shape["cpus"]), float(shape["memoryGb"]))


async def main() -> None:
    config = yaml.safe_load(Path(__file__).with_name("config.yaml").read_text())
    worker = worker_of(config)
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
