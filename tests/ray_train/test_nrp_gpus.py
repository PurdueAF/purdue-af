"""Tests for apps/ray-train/nrp_gpus.py and the gateway's `GET /af/nrp-gpus`,
against a fake of NRP's Prometheus."""

import sys
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from common import REPO, load_script

sys.path.insert(0, str(REPO / "apps" / "jupyterhub" / "jupyterhub" / "extraFiles"))
sys.path.insert(0, str(REPO / "apps" / "ray-train"))
nrp = load_script(REPO / "apps" / "ray-train" / "nrp_gpus.py", "nrp_gpus")
gw = load_script(REPO / "apps" / "ray-train" / "gateway.py", "ray_train_gateway")

GIB = 2**30
WORKER = nrp.Worker(gpus=2, cpus=8, memory_gb=24)


def sample(value, **labels):
    return {"metric": labels, "value": [0, str(value)]}


def node(name, gpus, cpus, memory_gb):
    return [
        sample(gpus, node=name, resource=nrp.GPU),
        sample(cpus, node=name, resource="cpu"),
        sample(memory_gb * GIB, node=name, resource="memory"),
    ]


# west-1: 4 of 8 GPUs free, room for two workers; west-2: free GPUs but cordoned;
# east-1: 2 GPUs free, too few cores for a worker; bare-1: no region label.
ALLOCATABLE = [
    *node("west-1", 8, 64, 512),
    *node("west-2", 4, 64, 512),
    *node("east-1", 4, 16, 128),
    *node("bare-1", 2, 32, 256),
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
REGIONS = [
    sample(1, node="west-1", label_topology_kubernetes_io_region="us-west"),
    sample(1, node="west-2", label_topology_kubernetes_io_region="us-west"),
    sample(1, node="east-1", label_topology_kubernetes_io_region="us-east"),
    sample(1, node="bare-1"),
]


def test_free_gpus_count_schedulable_nodes_and_workers_fit_their_cores_and_memory():
    regions = nrp.free_by_region(ALLOCATABLE, REQUESTED, UNSCHEDULABLE, REGIONS, WORKER)
    assert nrp.as_dict(regions) == {
        "unlabeled": {"gpus": 2, "free_gpus": 2, "workers": 1},
        "us-east": {"gpus": 4, "free_gpus": 2, "workers": 0},
        "us-west": {"gpus": 12, "free_gpus": 4, "workers": 2},
    }


def test_the_worker_shape_is_read_from_the_gateway_settings():
    assert gw.NRP_WORKER == nrp.worker_of(gw.CONFIG)
    assert gw.NRP_WORKER.gpus >= 1


@pytest.fixture
async def prometheus():
    state = SimpleNamespace(up=True)
    answers = {
        nrp.ALLOCATABLE: ALLOCATABLE,
        nrp.REQUESTED: REQUESTED,
        nrp.UNSCHEDULABLE: UNSCHEDULABLE,
        nrp.REGIONS: REGIONS,
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
    monkeypatch.setattr(gw, "NRP_WORKER", WORKER)
    async with ClientSession() as http:
        server = TestServer(gw.dashboard_app(SimpleNamespace(http=http, handle=refuse)))
        await server.start_server()
        yield str(server.make_url("")).rstrip("/")
        await server.close()


async def test_the_gateway_answers_nrp_gpus_without_a_token(dashboard):
    async with ClientSession() as session:
        async with session.get(f"{dashboard}/af/nrp-gpus") as response:
            assert response.status == 200
            assert (await response.json())["us-west"] == {
                "gpus": 12,
                "free_gpus": 4,
                "workers": 2,
            }


async def test_the_gateway_says_so_when_nrp_prometheus_does_not_answer(
    dashboard, prometheus
):
    prometheus.up = False
    async with ClientSession() as session:
        async with session.get(f"{dashboard}/af/nrp-gpus") as response:
            assert response.status == 503
