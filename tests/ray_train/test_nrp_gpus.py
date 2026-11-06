"""Tests for apps/ray-train/nrp_gpus.py and the gateway's `GET /af/nrp-gpus`,
against a fake of NRP's Prometheus."""

from types import SimpleNamespace

import pytest
import yaml
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from common import REPO, load_script, sample

nrp = load_script(REPO / "apps" / "ray-train" / "nrp_gpus.py", "nrp_gpus")
gw = load_script(REPO / "apps" / "ray-train" / "gateway.py", "ray_train_gateway")

GIB = 2**30
WORKER = nrp.Worker(gpus=2, cpus=8, memory_gb=24)


def allocatable(name, gpus, cpus, memory_gb):
    return [
        sample(gpus, node=name, resource=nrp.GPU),
        sample(cpus, node=name, resource="cpu"),
        sample(memory_gb * GIB, node=name, resource="memory"),
    ]


# west-1: 4 of 8 GPUs free, room for two workers, four empty; west-2: free GPUs but
# cordoned; east-1: 2 GPUs free, too few cores for a worker; bare-1: no DCGM.
ALLOCATABLE = [
    *allocatable("west-1", 8, 64, 512),
    *allocatable("west-2", 4, 64, 512),
    *allocatable("east-1", 4, 16, 128),
    *allocatable("bare-1", 2, 32, 256),
]
REQUESTED = [
    sample(4, node="west-1", resource=nrp.GPU),
    sample(40, node="west-1", resource="cpu"),
    sample(2, node="east-1", resource=nrp.GPU),
    sample(12, node="east-1", resource="cpu"),
    # Pending and unbound: holds nothing yet.
    sample(8, resource=nrp.GPU),
]
UNSCHEDULABLE = [sample(1, node="west-2")]
MODELS = [
    sample(1, Hostname="west-1", modelName="NVIDIA A10"),
    sample(1, Hostname="west-2", modelName="NVIDIA A10"),
    sample(1, Hostname="east-1", modelName="Tesla T4"),
]
# Of whole GPUs, in MiB.
MEMORY = [
    sample(23_028, modelName="NVIDIA A10"),
    sample(14_927, modelName="Tesla T4"),
    # A model on no node of ours.
    sample(81_182, modelName="NVIDIA H100 80GB HBM3"),
]


def models():
    return nrp.by_model(ALLOCATABLE, REQUESTED, UNSCHEDULABLE, MODELS, MEMORY, WORKER)


def test_free_gpus_count_schedulable_nodes_and_workers_fit_their_cores_and_memory():
    assert {
        name: (m.gpus, m.free_gpus, m.workers, m.workers_empty)
        for name, m in models().items()
    } == {
        "": (2, 2, 1, 1),
        "NVIDIA A10": (12, 4, 2, 4),
        "Tesla T4": (4, 2, 0, 2),
    }
    assert round(models()["Tesla T4"].memory_gb, 1) == 15.7
    assert models()[""].memory_gb is None


def test_a_job_of_any_gpu_may_take_every_node_with_room():
    assert nrp.room(models(), 0) == nrp.Room(3, 7, None)


def test_a_job_asking_for_gpu_memory_takes_the_models_that_report_nearly_as_much():
    # A T4 reports 15.7 GB of its 16; a node DCGM does not report has no known memory.
    assert nrp.room(models(), 16) == nrp.Room(2, 6, ["NVIDIA A10", "Tesla T4"])
    assert nrp.room(models(), 24) == nrp.Room(2, 4, ["NVIDIA A10"])
    assert nrp.room(models(), 80) == nrp.Room(0, 0, [])


def test_a_gpu_model_is_named_on_its_nodes_with_hyphens():
    assert nrp.product("NVIDIA GeForce RTX 2080 Ti") == "NVIDIA-GeForce-RTX-2080-Ti"


def test_the_worker_shape_is_the_nrp_templates_worker():
    template = yaml.safe_load((REPO / "apps/ray-train/rayjob-nrp.yaml").read_text())
    assert gw.nrp_worker() == nrp.worker_of(template) == nrp.Worker(1, 4, 16)


def test_a_new_template_sets_the_worker_the_gateway_counts(monkeypatch, tmp_path):
    text = (REPO / "apps/ray-train/rayjob-nrp.yaml").read_text()
    template = tmp_path / "rayjob-nrp.yaml"
    template.write_text(text.replace("memory: 16Gi", "memory: 20Gi"))
    monkeypatch.setattr(gw, "NRP_TEMPLATE", template)
    assert gw.nrp_worker().memory_gb == 20


@pytest.fixture
async def prometheus():
    state = SimpleNamespace(up=True)
    answers = {
        nrp.ALLOCATABLE: ALLOCATABLE,
        nrp.REQUESTED: REQUESTED,
        nrp.UNSCHEDULABLE: UNSCHEDULABLE,
        nrp.MODELS: MODELS,
        nrp.MEMORY: MEMORY,
    }

    async def query(request):
        if not state.up:
            return web.Response(status=503)
        return web.json_response(
            {"status": "success", "data": {"result": answers[request.query["query"]]}}
        )

    app = web.Application()
    app.router.add_get("/api/v1/query", query)
    server = TestServer(app)
    await server.start_server()
    state.url = str(server.make_url("")).rstrip("/")
    yield state
    await server.close()


async def refuse(request):
    return web.Response(status=404)


@pytest.fixture
async def dashboard(prometheus, monkeypatch):
    monkeypatch.setattr(gw, "NRP_PROMETHEUS_URL", prometheus.url)
    monkeypatch.setattr(gw, "nrp_worker", lambda: WORKER)
    async with ClientSession() as http:
        server = TestServer(
            gw.dashboard_app(
                SimpleNamespace(http=http, handle=refuse, own_cluster=refuse)
            )
        )
        await server.start_server()
        yield str(server.make_url("")).rstrip("/")
        await server.close()


async def test_the_gateway_answers_nrp_gpus_without_a_token(dashboard):
    async with ClientSession() as session:
        async with session.get(f"{dashboard}/af/nrp-gpus") as response:
            assert response.status == 200
            found = (await response.json())["NVIDIA A10"]
            assert (found["gpus"], found["free_gpus"], found["workers"]) == (12, 4, 2)


async def test_the_gateway_answers_capacity_without_a_token(dashboard):
    async def capacity(values):
        return {"asked": dict(values)}

    app = gw.dashboard_app(
        SimpleNamespace(http=None, handle=refuse, own_cluster=refuse, capacity=capacity)
    )
    server = TestServer(app)
    await server.start_server()
    async with ClientSession() as session:
        url = server.make_url("/af/capacity?af-gpus-per-worker=2")
        async with session.get(url) as response:
            assert response.status == 200
            assert await response.json() == {"asked": {"af-gpus-per-worker": "2"}}
    await server.close()


async def test_the_gateway_says_why_it_refuses_a_shape():
    async def capacity(values):
        raise gw.Refused(gw.grpc.StatusCode.INVALID_ARGUMENT, "No such GPU")

    app = gw.dashboard_app(
        SimpleNamespace(http=None, handle=refuse, own_cluster=refuse, capacity=capacity)
    )
    server = TestServer(app)
    await server.start_server()
    async with ClientSession() as session:
        async with session.get(server.make_url("/af/capacity")) as response:
            assert (response.status, response.reason) == (400, "No such GPU")
    await server.close()


async def test_the_gateway_says_so_when_nrp_prometheus_does_not_answer(
    dashboard, prometheus
):
    prometheus.up = False
    async with ClientSession() as session:
        async with session.get(f"{dashboard}/af/nrp-gpus") as response:
            assert response.status == 503
