"""The launch command helmrelease.yaml gives the Dask Gateway controller."""

import sys
import types

import pytest
import yaml
from common import REPO

RELEASE = REPO / "apps" / "dask-gateway" / "helmrelease.yaml"
GIB = 2**30


class KubeController:
    async def batch_create_pods(self, info, namespace, pod, count):
        priority = pod["spec"].get("priorityClassName")
        self.created.append((priority, count))
        return priority in self.failing


def get_container_status(pod, name):
    for status in pod["status"].get("containerStatuses", ()):
        if status["name"] == name:
            return status
    return None


@pytest.fixture
def launch(monkeypatch):
    """Exec the launch command against stub dask_gateway_server modules."""
    release = yaml.safe_load(RELEASE.read_text())
    patches = release["spec"]["postRenderers"][0]["kustomize"]["patches"]
    (patch,) = (p["patch"] for p in patches if p["target"]["name"] == "controller-.*")
    (op,) = yaml.safe_load(patch)
    *python, code = op["value"]
    assert python == ["python", "-c"]

    launched = []
    app = types.ModuleType("dask_gateway_server.app")
    app.main = launched.append
    kubernetes = types.ModuleType("dask_gateway_server.backends.kubernetes")
    kubernetes.controller = types.ModuleType(f"{kubernetes.__name__}.controller")
    kubernetes.controller.KubeController = type("KubeController", (KubeController,), {})
    kubernetes.controller.get_container_status = get_container_status
    for module in (app, kubernetes):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    ns = {}
    exec(compile(code, f"{RELEASE}:controller args", "exec"), ns)
    ns["launched"] = launched
    return ns


async def scale_up(
    launch, worker_cores, floor_live, burst_live, count, failing=(), worker_gib=1
):
    """The (priority class, pod count) batches a scale-up by `count` creates."""
    burst = {"priorityClassName": launch["PREEMPTIBLE"]}
    live = [{}] * floor_live + [burst] * burst_live
    names = [str(i) for i in range(len(live))]
    controller = launch["controller"].KubeController()
    controller.informers = {
        "pod": {f"cms.{name}": {"spec": spec} for name, spec in zip(names, live)}
    }
    controller.created, controller.failing = [], failing
    info = types.SimpleNamespace(running=set(names[::2]), pending=set(names[1::2]))
    requests = {"cpu": f"{worker_cores:.3f}", "memory": str(int(worker_gib * GIB))}
    pod = {"spec": {"containers": [{"resources": {"requests": requests}}]}}
    failed = await controller.batch_create_pods(info, "cms", pod, count)
    return controller.created, failed


def test_controller_starts_after_the_hook(launch):
    assert [argv[0] for argv in launch["launched"]] == ["kube-controller"]


async def test_first_floor_cores_keep_default_priority(launch):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    created, failed = await scale_up(launch, 1, 0, 0, floor + 50)
    assert created == [(None, floor), (preemptible, 50)]
    assert not failed


async def test_floor_is_counted_in_cores(launch):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    created, _ = await scale_up(launch, 4, 0, 0, floor)
    assert created == [(None, floor // 4), (preemptible, floor - floor // 4)]


async def test_floor_is_counted_in_memory(launch):
    preemptible = launch["PREEMPTIBLE"]
    tenth = launch["FLOOR_MEMORY"] / GIB / 10
    created, _ = await scale_up(launch, 1, 0, 0, 50, worker_gib=tenth)
    assert created == [(None, 10), (preemptible, 40)]


async def test_floor_is_counted_in_workers(launch):
    floor, preemptible = launch["FLOOR_WORKERS"], launch["PREEMPTIBLE"]
    created, _ = await scale_up(launch, 0.1, 0, 0, floor + 50, worker_gib=0.1)
    assert created == [(None, floor), (preemptible, 50)]


async def test_lost_floor_workers_are_replaced_first(launch):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    created, _ = await scale_up(launch, 1, floor - 10, 30, 25)
    assert created == [(None, 10), (preemptible, 15)]


async def test_full_floor_adds_only_preemptible_workers(launch):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    created, _ = await scale_up(launch, 1, floor, 0, 7)
    assert created == [(None, 0), (preemptible, 7)]


@pytest.mark.parametrize("tier", ["floor", "burst"])
async def test_a_failed_tier_requeues_the_cluster(launch, tier):
    failing = (None if tier == "floor" else launch["PREEMPTIBLE"],)
    _, failed = await scale_up(launch, 1, 0, 0, launch["FLOOR_CORES"] + 1, failing)
    assert failed


@pytest.mark.parametrize("phase", ["Pending", "Succeeded", "Failed"])
def test_worker_pod_is_tracked_before_it_has_a_container_status(launch, phase):
    status = launch["controller"].get_container_status
    assert status({"status": {"phase": phase}}, "dask-worker") is not None


def test_container_status_is_upstreams_otherwise(launch):
    status = launch["controller"].get_container_status
    reported = {"name": "dask-worker", "state": {"running": {}}}
    running = {"status": {"phase": "Running", "containerStatuses": [reported]}}
    assert status(running, "dask-worker") is reported
    assert status({"status": {"phase": "Running"}}, "dask-worker") is None
    assert status({"status": {"phase": "Failed"}}, "dask-scheduler") is None
