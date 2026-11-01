"""Tests for apps/ray-train/nrp_gpus.py and the gateway's `GET /af/nrp-gpus`,
against a fake of NRP's Prometheus."""

import sys
from types import SimpleNamespace

import pytest
import yaml
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


def test_the_worker_shape_is_the_nrp_templates_worker():
    template = yaml.safe_load((REPO / "apps/ray-train/rayjob-nrp.yaml").read_text())
    assert gw.NRP_WORKER == nrp.worker_of(template) == nrp.Worker(1, 4, 16)


MODELS = [
    sample(1, Hostname="west-1", modelName="NVIDIA A10"),
    sample(1, Hostname="west-2", modelName="NVIDIA A10"),
    sample(1, Hostname="east-1", modelName="Tesla V100-SXM2-16GB"),
    sample(1, Hostname="bare-1", modelName="NVIDIA A10"),
]


def test_open_nodes_have_room_a_region_and_a_gpu_model():
    nodes = nrp.open_nodes(
        ALLOCATABLE, REQUESTED, UNSCHEDULABLE, REGIONS, MODELS, WORKER
    )
    # west-2 is cordoned, east-1 has too few cores left, bare-1 is of no region.
    assert nodes == [
        nrp.Node("bare-1", "unlabeled", "NVIDIA A10", 1),
        nrp.Node("west-1", "us-west", "NVIDIA A10", 2),
    ]


def node(name, workers, region="us-west", model="NVIDIA A10"):
    return nrp.Node(name, region, model, workers)


def test_a_job_is_placed_where_one_region_and_model_has_the_most_room():
    nodes = [
        node("a", 1),
        node("b", 3),
        node("c", 2),
        node("d", 5, region="us-east"),
        node("e", 4, model="NVIDIA L40"),
    ]
    assert nrp.most_in_one_group(nodes) == 6
    # Every node of that region and model: another job
    # placed from the same reading may take the room of one.
    group = [node("a", 1), node("b", 3), node("c", 2)]
    assert nrp.placement(nodes, least=1) == group
    assert nrp.placement(nodes, least=2) == group
    assert nrp.placement(nodes, least=7) is None
    assert nrp.placement([], least=1) is None


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
            assert (await response.json())["us-west"] == {
                "gpus": 12,
                "free_gpus": 4,
                "workers": 2,
            }


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
