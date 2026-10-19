"""The launch command helmrelease.yaml gives the Dask Gateway controller."""

import asyncio
import logging
import sys
import types

import pytest
import yaml
from aiohttp import web
from common import REPO

RELEASE = REPO / "apps" / "dask-gateway" / "helmrelease.yaml"
GIB = 2**30


class KubeController:
    async def batch_create_pods(self, info, namespace, pod, count):
        priority = pod["spec"].get("priorityClassName")
        self.created.append((priority, count))
        return priority in self.failing

    async def reconcile_cluster(self, key):
        self.reconciled.append(key)
        return True

    async def setup(self):
        """Upstream starts the web app here, after which no route can be added."""
        self.app.freeze()
        self.queue = types.SimpleNamespace(closed=False, put=self.queued.append)
        self.reconcilers = [
            asyncio.ensure_future(asyncio.sleep(3600)) for _ in range(2)
        ]
        self.task_pool = types.SimpleNamespace(spawn=self.spawned.append)


def get_container_status(pod, name):
    for status in pod["status"].get("containerStatuses", ()):
        if status["name"] == name:
            return status
    return None


def controller_ops(release):
    patches = release["spec"]["postRenderers"][0]["kustomize"]["patches"]
    (patch,) = (p["patch"] for p in patches if p["target"]["name"] == "controller-.*")
    return yaml.safe_load(patch)


@pytest.fixture
def launch(monkeypatch):
    """Exec the launch command against stub dask_gateway_server modules."""
    release = yaml.safe_load(RELEASE.read_text())
    (op,) = (op for op in controller_ops(release) if op["path"].endswith("/args"))
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
    launch,
    worker_cores,
    floor_live,
    burst_live,
    count,
    failing=(),
    worker_gib=1,
    quotas=(),
):
    """The (priority class, pod count) batches a scale-up by `count` creates,
    whether it failed, and the creations the cluster still expects to see.

    `quotas` are the (hard, used) of the namespace's ResourceQuotas.
    """
    burst = {"priorityClassName": launch["PREEMPTIBLE"]}
    live = [{}] * floor_live + [burst] * burst_live
    names = [str(i) for i in range(len(live))]
    controller = launch["controller"].KubeController()
    controller.informers = {
        "pod": {f"cms.{name}": {"spec": spec} for name, spec in zip(names, live)}
    }
    controller.created, controller.failing = [], failing

    async def list_namespaced_resource_quota(namespace):
        statuses = (types.SimpleNamespace(hard=h, used=u) for h, u in quotas)
        return types.SimpleNamespace(
            items=[types.SimpleNamespace(status=status) for status in statuses]
        )

    controller.core_client = types.SimpleNamespace(
        list_namespaced_resource_quota=list_namespaced_resource_quota
    )
    info = types.SimpleNamespace(
        running=set(names[::2]), pending=set(names[1::2]), creates=count
    )
    resources = {"cpu": f"{worker_cores:.3f}", "memory": str(int(worker_gib * GIB))}
    pod = {
        "spec": {
            "containers": [{"resources": {"requests": resources, "limits": resources}}]
        }
    }
    failed = await controller.batch_create_pods(info, "cms", pod, count)
    return controller.created, failed, info.creates


def test_controller_starts_after_the_hook(launch):
    assert [argv[0] for argv in launch["launched"]] == ["kube-controller"]


async def test_first_floor_cores_keep_default_priority(launch):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    created, failed, _ = await scale_up(launch, 1, 0, 0, floor + 50)
    assert created == [(None, floor), (preemptible, 50)]
    assert not failed


async def test_floor_is_counted_in_cores(launch):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    created, *_ = await scale_up(launch, 4, 0, 0, floor)
    assert created == [(None, floor // 4), (preemptible, floor - floor // 4)]


async def test_floor_is_counted_in_memory(launch):
    preemptible = launch["PREEMPTIBLE"]
    tenth = launch["FLOOR_MEMORY"] / GIB / 10
    created, *_ = await scale_up(launch, 1, 0, 0, 50, worker_gib=tenth)
    assert created == [(None, 10), (preemptible, 40)]


async def test_floor_is_counted_in_workers(launch):
    floor, preemptible = launch["FLOOR_WORKERS"], launch["PREEMPTIBLE"]
    created, *_ = await scale_up(launch, 0.1, 0, 0, floor + 50, worker_gib=0.1)
    assert created == [(None, floor), (preemptible, 50)]


async def test_lost_floor_workers_are_replaced_first(launch):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    created, *_ = await scale_up(launch, 1, floor - 10, 30, 25)
    assert created == [(None, 10), (preemptible, 15)]


async def test_full_floor_adds_only_preemptible_workers(launch):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    created, *_ = await scale_up(launch, 1, floor, 0, 7)
    assert created == [(None, 0), (preemptible, 7)]


@pytest.mark.parametrize("tier", ["floor", "burst"])
async def test_a_failed_tier_requeues_the_cluster(launch, tier):
    failing = (None if tier == "floor" else launch["PREEMPTIBLE"],)
    _, failed, _ = await scale_up(launch, 1, 0, 0, launch["FLOOR_CORES"] + 1, failing)
    assert failed


async def test_burst_leaves_the_quota_reserve_free(launch):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    room = int(100 * (1 - launch["QUOTA_RESERVE"]) - 39.5)
    quota = (
        {"requests.memory": "100Gi", "persistentvolumeclaims": "1"},
        {"requests.memory": "39.5Gi"},
    )
    created, failed, expected = await scale_up(
        launch, 1, floor, 0, room + 5, quotas=[quota]
    )
    assert created == [(None, 0), (preemptible, room)]
    assert failed
    assert expected == room


async def test_burst_fits_while_the_reserve_stays_free(launch):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    quota = ({"requests.memory": "100Gi"}, {"requests.memory": "40Gi"})
    created, failed, expected = await scale_up(launch, 1, floor, 0, 5, quotas=[quota])
    assert created == [(None, 0), (preemptible, 5)]
    assert not failed
    assert expected == 5


async def test_guaranteed_workers_are_not_held_to_the_reserve(launch):
    preemptible = launch["PREEMPTIBLE"]
    full = ({"requests.memory": "100Gi"}, {"requests.memory": "100Gi"})
    created, failed, _ = await scale_up(launch, 1, 0, 0, 10, quotas=[full])
    assert created == [(None, 10), (preemptible, 0)]
    assert not failed


async def test_no_burst_starts_inside_the_reserve(launch):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    full = ({"requests.memory": "100Gi"}, {"requests.memory": "104857600Ki"})
    created, failed, expected = await scale_up(launch, 1, floor, 0, 7, quotas=[full])
    assert created == [(None, 0), (preemptible, 0)]
    assert failed
    assert expected == 0


@pytest.mark.parametrize(
    "tight",
    ["requests.cpu", "limits.cpu", "requests.memory", "limits.memory", "pods"],
)
async def test_the_tightest_quota_bounds_the_burst(launch, tight):
    floor, preemptible = launch["FLOOR_CORES"], launch["PREEMPTIBLE"]
    keep = 1 - launch["QUOTA_RESERVE"]
    worker = {"cpu": 2, "memory": 4 * GIB, "pods": 1}

    def quota(*names):
        """(hard, used) with room for 3 workers under `tight`, for 50 under the rest."""
        per = {name: worker[name.split(".")[-1]] for name in names}
        hard = {name: 100 * per[name] for name in names}
        room = {name: 3.5 if name == tight else 50.5 for name in names}
        used = {name: hard[name] * keep - room[name] * per[name] for name in names}
        return tuple({n: str(v) for n, v in q.items()} for q in (hard, used))

    quotas = [
        quota("requests.cpu", "limits.cpu", "pods"),
        quota("requests.memory", "limits.memory"),
    ]
    created, *_ = await scale_up(launch, 2, floor, 0, 20, worker_gib=4, quotas=quotas)
    assert created == [(None, 0), (preemptible, 3)]


@pytest.mark.parametrize(
    "text, value",
    [
        ("2656", 2656),
        ("8.000", 8),
        ("1675800m", 1675.8),
        ("7787440552960", 7787440552960),
        ("7600859183Ki", 7600859183 * 2**10),
        ("7428236Mi", 7428236 * 2**20),
        ("6T", 6e12),
        ("1e3", 1000),
    ],
)
def test_quantity_reads_kubernetes_units(launch, text, value):
    assert launch["quantity"](text) == pytest.approx(value)


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


@pytest.fixture
async def started(launch):
    """A controller past the launch command's setup."""
    controller = launch["controller"].KubeController()
    controller.app = web.Application()
    controller.app["controller"] = controller
    controller.log = logging.getLogger("dask-gateway-controller")
    controller.reconciled, controller.queued, controller.spawned = [], [], []
    await controller.setup()
    yield controller
    for task in controller.reconcilers:
        task.cancel()
    for coroutine in controller.spawned:
        coroutine.close()


async def health(controller):
    """The status the controller's /healthz answers with."""
    routes = controller.app.router.routes()
    (route,) = (
        r for r in routes if (r.method, r.resource.canonical) == ("GET", "/healthz")
    )
    response = await route.handler(types.SimpleNamespace(app=controller.app))
    return response.status


async def stop(task):
    """Cancel a task and return once its done callbacks have run."""
    task.cancel()
    await asyncio.wait([task])


async def test_heartbeat_is_queued_with_the_clusters(launch, started):
    (beat,) = started.spawned
    task = asyncio.ensure_future(beat)
    await asyncio.sleep(0)
    await stop(task)
    assert started.queued == [launch["HEARTBEAT"]]


async def test_heartbeat_never_reaches_a_cluster(launch, started):
    started.last_beat = 0
    assert await started.reconcile_cluster(launch["HEARTBEAT"]) is None
    assert started.last_beat > 0
    assert await started.reconcile_cluster("cms.cluster") is True
    assert started.reconciled == ["cms.cluster"]


async def test_health_passes_while_heartbeats_get_through(started):
    assert await health(started) == 200


async def test_health_fails_once_the_heartbeat_is_stale(launch, started):
    started.last_beat -= launch["STALL"]
    assert await health(started) == 503


async def test_an_exited_reconciler_fails_health_and_logs(started, caplog):
    await stop(started.reconcilers[0])
    assert await health(started) == 503
    assert [r.levelno for r in caplog.records] == [logging.ERROR]


async def test_shutdown_logs_no_exited_reconciler(started, caplog):
    started.queue.closed = True
    await stop(started.reconcilers[0])
    assert not caplog.records


def test_probes_read_the_health_route():
    release = yaml.safe_load(RELEASE.read_text())
    probes = {
        op["path"].rsplit("/", 1)[1]: op["value"] for op in controller_ops(release)
    }
    for kind in ("livenessProbe", "readinessProbe"):
        assert probes[kind]["httpGet"] == {"path": "/healthz", "port": "api"}, kind
