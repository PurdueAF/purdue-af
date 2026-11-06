"""The GPUs free on NRP Nautilus for Ray workers of ours, by GPU model, from NRP's
public Prometheus: those of nodes that are Ready, not cordoned and carry no
NoSchedule or NoExecute taint, less what the pods bound to them request, and
how many workers of a shape fit beside them; and the GPU models with at least
the memory a job asks for. An upper bound: NRP's scheduler also weighs
affinities and policies that Prometheus does not show.

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
# DCGM names a node's GPU model, which no kube-state-metrics label does.
MODELS = "count by (Hostname, modelName) (max_over_time(DCGM_FI_DEV_GPU_UTIL[1h]))"
# The memory of each model's whole GPUs, in MiB: the median, as single readings stray.
MEMORY = (
    "quantile by (modelName) (0.5,"
    ' sum by (modelName, UUID) (DCGM_FI_DEV_FB_FREE{GPU_I_ID=""})'
    ' + on(modelName, UUID) sum by (modelName, UUID) (DCGM_FI_DEV_FB_USED{GPU_I_ID=""}))'
)
# A GPU reports less memory than it is sold with: a 16 GB T4 reports 15.6 GB.
MEMORY_SLACK = 0.95
# The node label NVIDIA's feature discovery names a node's GPU model by.
PRODUCT_LABEL = "nvidia.com/gpu.product"
TIMEOUT = ClientTimeout(total=60)


@dataclass(frozen=True)
class Worker:
    """The requests of one Ray worker pod."""

    gpus: int
    cpus: float
    memory_gb: float


@dataclass
class Model:
    """The GPUs of one model: their memory in GB, how many, how many are free,
    and how many workers fit beside what holds them now, and on empty nodes."""

    memory_gb: float | None = None
    gpus: int = 0
    free_gpus: int = 0
    workers: int = 0
    workers_empty: int = 0


@dataclass(frozen=True)
class Room:
    """The workers that fit now and on empty nodes, of the GPU models a job may
    hold: None for any GPU."""

    workers: int
    workers_empty: int
    models: list[str] | None


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


def by_model(
    allocatable: list[dict[str, Any]],
    requested: list[dict[str, Any]],
    unschedulable: list[dict[str, Any]],
    models: list[dict[str, Any]],
    memory: list[dict[str, Any]],
    worker: Worker,
) -> dict[str, Model]:
    """Prometheus's results of the five queries, summed by GPU model; a node DCGM
    does not report is of the model ""."""
    free, held, closed = free_by_node(allocatable, requested, unschedulable)
    model_of = {s["metric"]["Hostname"]: s["metric"]["modelName"] for s in models}
    out: dict[str, Model] = defaultdict(Model)
    for s in memory:
        out[s["metric"]["modelName"]].memory_gb = float(s["value"][1]) * 2**20 / 1e9
    for node, has in free.items():
        model = out[model_of.get(node, "")]
        model.gpus += int(has[GPU])
        if node in closed:
            continue
        model.free_gpus += max(int(has[GPU] - held[node][GPU]), 0)
        model.workers += fitting(has, held[node], worker)
        model.workers_empty += fitting(has, defaultdict(float), worker)
    return {name: model for name, model in sorted(out.items()) if model.gpus}


def with_memory(models: dict[str, Model], memory_gb: float) -> list[str] | None:
    """The models whose GPUs have `memory_gb`; None, for any GPU, if it is 0."""
    if not memory_gb:
        return None
    return [
        name
        for name, model in models.items()
        if name
        and model.memory_gb is not None
        and model.memory_gb >= memory_gb * MEMORY_SLACK
    ]


def room(models: dict[str, Model], memory_gb: float) -> Room:
    """The workers that fit on GPUs of `memory_gb` or more, now and empty."""
    allowed = with_memory(models, memory_gb)
    held = [m for name, m in models.items() if allowed is None or name in allowed]
    return Room(
        sum(m.workers for m in held), sum(m.workers_empty for m in held), allowed
    )


def product(model: str) -> str:
    """A GPU model's value of PRODUCT_LABEL: its name with hyphens for spaces."""
    return model.replace(" ", "-")


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
) -> dict[str, Model]:
    allocatable, requested, unschedulable, models, memory = await asyncio.gather(
        *(
            query(http, prometheus, promql)
            for promql in (ALLOCATABLE, REQUESTED, UNSCHEDULABLE, MODELS, MEMORY)
        )
    )
    return by_model(allocatable, requested, unschedulable, models, memory, worker)


def worker_of(template: dict[str, Any]) -> Worker:
    """The requests of the template's worker."""
    spec = template["spec"]["rayClusterSpec"]
    (group,) = spec["workerGroupSpecs"]
    (container,) = group["template"]["spec"]["containers"]
    requests = container["resources"]["requests"]
    memory = str(requests["memory"])
    return Worker(
        int(requests["nvidia.com/gpu"]),
        float(requests["cpu"]),
        float(memory.removesuffix("Gi")),
    )


def as_dict(models: dict[str, Model]) -> dict[str, dict[str, Any]]:
    return {name: dataclasses.asdict(model) for name, model in models.items()}


async def main() -> None:
    here = Path(__file__).parent
    config = yaml.safe_load((here / "config.yaml").read_text())
    worker = worker_of(yaml.safe_load((here / "rayjob-nrp.yaml").read_text()))
    async with ClientSession() as http:
        models = await free_gpus(http, config["nrpPrometheusUrl"], worker)
    print(
        f"{'model':<44}{'GB':>6}{'GPUs':>7}{'free':>7}{'workers':>9}"
        f"   (a worker: {worker.gpus} GPUs, {worker.cpus:g} CPUs, {worker.memory_gb:g} GB)"
    )
    for name, model in models.items():
        memory = "" if model.memory_gb is None else f"{model.memory_gb:.0f}"
        print(
            f"{name or 'unknown':<44}{memory:>6}{model.gpus:>7}{model.free_gpus:>7}{model.workers:>9}"
        )


if __name__ == "__main__":
    asyncio.run(main())
