"""Tests for tools/dask.py — gateway resolution and cluster operations."""

import base64
import json

import pytest
import respx
from agentic_helpers import register_tools
from httpx import ConnectError
from tools import dask

K8S = dask._GATEWAYS["k8s"]
SLURM = dask._GATEWAYS["slurm"]


def clusters_url(base):
    return f"{base}/api/v1/clusters/"


def clusters_payload(*clusters):
    """Match Dask Gateway's GET /api/v1/clusters/ shape: {name: model, …}."""
    return {c["name"]: c for c in clusters}


def basic_alice():
    return "Basic " + base64.b64encode(b"alice:").decode()


class _FakeResult:
    def __init__(self, action, data=None):
        self.action = action
        self.data = data


class FakeCtx:
    """Stand-in for FastMCP Context; scripts elicit() responses in order."""

    def __init__(self, *responses):
        # each response: (action, data) where data is a schema instance
        self._responses = list(responses)
        self.calls = []

    async def elicit(self, message, schema):
        self.calls.append((message, schema))
        if not self._responses:
            raise AssertionError(f"unexpected elicit call: {message!r}")
        action, data = self._responses.pop(0)
        return _FakeResult(action, data)


def accept(data):
    return ("accept", data)


# ── _resolve_gateway ──────────────────────────────────────────────────────────


def test_resolve_canonical_names():
    for name in ("k8s", "slurm"):
        canonical, url = dask._resolve_gateway(name)
        assert canonical == name
        assert url == dask._GATEWAYS[name]


def test_resolve_is_case_insensitive():
    assert dask._resolve_gateway("K8S")[0] == "k8s"
    assert dask._resolve_gateway("Slurm")[0] == "slurm"


def test_resolve_unknown_raises():
    with pytest.raises(ValueError, match="Unknown gateway"):
        dask._resolve_gateway("nope")
    with pytest.raises(ValueError, match="Unknown gateway"):
        dask._resolve_gateway("slurm-hammer")


# ── _fmt_cluster / _parse_clusters ────────────────────────────────────────────


def test_fmt_cluster_fixed_workers():
    out = dask._fmt_cluster(
        {"name": "c1", "status": "RUNNING", "workers": {"w1": {}, "w2": {}}}, "k8s"
    )
    assert "workers=2" in out
    assert "gateway=k8s" in out


def test_fmt_cluster_adaptive():
    out = dask._fmt_cluster(
        {"name": "c1", "status": "RUNNING", "adaptive": {"minimum": 1, "maximum": 10}},
        "k8s",
    )
    assert "adaptive(1–10)" in out


def test_fmt_cluster_scheduler_address():
    out = dask._fmt_cluster(
        {"name": "c1", "status": "RUNNING", "scheduler_address": "tls://x:8786"}, "k8s"
    )
    assert "scheduler: tls://x:8786" in out


def test_parse_clusters_dict_keyed_by_name():
    payload = clusters_payload(
        {"name": "a", "status": "RUNNING"},
        {"name": "b", "status": "RUNNING"},
    )
    assert [c["name"] for c in dask._parse_clusters(payload)] == ["a", "b"]


def test_parse_clusters_empty():
    assert dask._parse_clusters({}) == []
    assert dask._parse_clusters(None) == []


# ── list_dask_clusters ────────────────────────────────────────────────────────


@respx.mock
async def test_list_clusters_aggregates_gateways(user_ctx):
    respx.get(clusters_url(K8S)).respond(
        200,
        json=clusters_payload({"name": "c-k8s", "status": "RUNNING", "workers": {}}),
    )
    respx.get(clusters_url(SLURM)).mock(side_effect=ConnectError("down"))

    tools = register_tools(dask).tools
    out = await tools["list_dask_clusters"]()

    assert "# 1 Dask cluster(s)" in out
    assert "c-k8s" in out
    assert "[slurm] error: unreachable" in out


@respx.mock
async def test_list_clusters_all_empty(user_ctx):
    for base in (K8S, SLURM):
        respx.get(clusters_url(base)).respond(200, json={})

    tools = register_tools(dask).tools
    assert await tools["list_dask_clusters"]() == (
        "No running Dask clusters on any gateway."
    )


@respx.mock
async def test_list_clusters_sends_basic_username(user_ctx):
    routes = [
        respx.get(clusters_url(base)).respond(200, json={}) for base in (K8S, SLURM)
    ]

    tools = register_tools(dask).tools
    await tools["list_dask_clusters"]()

    for route in routes:
        assert route.calls.last.request.headers["Authorization"] == basic_alice()


# ── get_dask_cluster_info ─────────────────────────────────────────────────────


@respx.mock
async def test_cluster_info_renders_details(user_ctx):
    workers = {f"w{i}": {"status": "running"} for i in range(25)}
    respx.get(f"{K8S}/api/v1/clusters/c1").respond(
        200,
        json={
            "name": "c1",
            "status": "RUNNING",
            "workers": workers,
            "options": {"worker_cores": 2},
        },
    )

    tools = register_tools(dask).tools
    out = await tools["get_dask_cluster_info"]("c1")

    assert "Workers (25):" in out
    assert "… 5 more" in out  # truncated at 20
    assert "worker_cores: 2" in out


@respx.mock
async def test_cluster_info_not_found(user_ctx):
    respx.get(f"{K8S}/api/v1/clusters/ghost").respond(404)

    tools = register_tools(dask).tools
    out = await tools["get_dask_cluster_info"]("ghost")
    assert "not found" in out


async def test_cluster_info_invalid_gateway(user_ctx):
    tools = register_tools(dask).tools
    out = await tools["get_dask_cluster_info"]("c1", gateway="bogus")
    assert "Unknown gateway" in out


# ── scale_dask_cluster ────────────────────────────────────────────────────────


async def test_scale_rejects_negative(user_ctx):
    tools = register_tools(dask).tools
    out = await tools["scale_dask_cluster"]("c1", -1)
    assert "must be ≥ 0" in out


@respx.mock
async def test_scale_posts_count(user_ctx):
    route = respx.post(f"{K8S}/api/v1/clusters/c1/scale").respond(200)

    tools = register_tools(dask).tools
    out = await tools["scale_dask_cluster"]("c1", 8)

    assert json.loads(route.calls.last.request.content) == {"count": 8}
    assert route.calls.last.request.headers["Authorization"] == basic_alice()
    assert "scaling to 8 worker(s)" in out


@respx.mock
async def test_scale_not_found(user_ctx):
    respx.post(f"{SLURM}/api/v1/clusters/ghost/scale").respond(404)

    tools = register_tools(dask).tools
    out = await tools["scale_dask_cluster"]("ghost", 2, gateway="slurm")
    assert "not found on gateway 'slurm'" in out


# ── _build_cluster_options / create / options ─────────────────────────────────


def test_build_options_pixi():
    opts = dask._build_cluster_options(
        username="alice",
        pixi_project="/depot/cms/alice/proj",
        pixi_env="analysis",
        conda_env=None,
        worker_cores=2,
        worker_memory=8,
        env={"X509_USER_PROXY": "/tmp/x509"},
    )
    assert opts["pixi_project"] == "/depot/cms/alice/proj"
    assert opts["pixi_env"] == "analysis"
    assert opts["conda_env"] == ""
    assert opts["worker_cores"] == 2
    assert opts["worker_memory"] == 8
    assert opts["env"]["PATH"]
    assert opts["env"]["HOME"] == "/home/alice"
    assert opts["env"]["X509_USER_PROXY"] == "/tmp/x509"


def test_build_options_conda():
    opts = dask._build_cluster_options(
        username="alice",
        pixi_project=None,
        pixi_env="default",
        conda_env="/depot/cms/alice/miniconda3/envs/ana",
        worker_cores=1,
        worker_memory=4,
        env=None,
    )
    assert opts["conda_env"] == "/depot/cms/alice/miniconda3/envs/ana"
    assert opts["pixi_project"] == ""


def test_build_options_rejects_both_or_neither():
    both = dask._build_cluster_options(
        username="alice",
        pixi_project="/p",
        pixi_env="default",
        conda_env="/c",
        worker_cores=1,
        worker_memory=1,
        env=None,
    )
    assert "mutually exclusive" in both

    neither = dask._build_cluster_options(
        username="alice",
        pixi_project=None,
        pixi_env="default",
        conda_env=None,
        worker_cores=1,
        worker_memory=1,
        env=None,
    )
    assert "provide either" in neither


@respx.mock
async def test_list_cluster_options(user_ctx):
    respx.get(f"{K8S}/api/v1/options").respond(
        200,
        json={
            "cluster_options": [
                {
                    "field": "worker_cores",
                    "label": "Cores per worker",
                    "default": 1,
                    "spec": {"type": "int", "min": 1, "max": 64},
                },
                {
                    "field": "pixi_project",
                    "label": "Pixi project",
                    "default": "",
                    "spec": {"type": "string"},
                },
            ]
        },
    )

    tools = register_tools(dask).tools
    out = await tools["list_dask_cluster_options"]()
    assert "gateway=k8s" in out
    assert "Kubernetes (Geddes)" in out
    assert "worker_cores" in out
    assert "pixi_project" in out


@respx.mock
async def test_create_cluster_explicit_args_pixi_then_scale(user_ctx):
    create = respx.post(clusters_url(K8S)).respond(201, json={"name": "cms.abc"})
    scale = respx.post(f"{K8S}/api/v1/clusters/cms.abc/scale").respond(204)

    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](
        FakeCtx(),  # explicit args → no elicitation
        gateway="k8s",
        pixi_project="/work/alice/proj",
        worker_cores=2,
        worker_memory=8,
        n_workers=3,
    )
    assert "cms.abc" in out
    assert "Scaling to 3" in out
    assert create.called
    opts = json.loads(create.calls[0].request.content)["cluster_options"]
    assert opts["pixi_project"] == "/work/alice/proj"
    assert opts["conda_env"] == ""
    assert opts["worker_cores"] == 2
    assert opts["env"]["USER"] == "alice"
    assert scale.called
    assert json.loads(scale.calls[0].request.content) == {"count": 3}


@respx.mock
async def test_create_cluster_explicit_slurm_conda_no_scale(user_ctx):
    create = respx.post(clusters_url(SLURM)).respond(201, json={"name": "cms.slurm1"})

    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](
        FakeCtx(),
        gateway="slurm",
        conda_env="/depot/cms/alice/envs/ana",
    )
    assert "slurm" in out
    assert "0 workers" in out
    assert create.called
    opts = json.loads(create.calls[0].request.content)["cluster_options"]
    assert opts["conda_env"] == "/depot/cms/alice/envs/ana"
    assert opts["pixi_project"] == ""


@respx.mock
async def test_create_cluster_gateway_rejects(user_ctx):
    respx.post(clusters_url(K8S)).respond(
        422, json={"message": "User already has 1 active clusters"}
    )

    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](
        FakeCtx(), gateway="k8s", pixi_project="/work/alice/p"
    )
    assert "rejected" in out
    assert "active clusters" in out


# ── create_dask_cluster: elicitation flows ────────────────────────────────────


@respx.mock
async def test_create_elicits_backend_and_global_env(user_ctx):
    create = respx.post(clusters_url(K8S)).respond(201, json={"name": "cms.g"})

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
    )
    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](ctx)

    assert "cms.g" in out
    assert create.called
    opts = json.loads(create.calls[0].request.content)["cluster_options"]
    assert opts["pixi_project"] == dask.GLOBAL_PIXI_PROJECT
    assert len(ctx.calls) == 2


@respx.mock
async def test_create_global_on_slurm_rejected(user_ctx):
    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="slurm")),
        accept(dask._EnvChoice(env_source="global")),
    )
    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](ctx)

    assert "/work" in out
    assert "Slurm" in out


@respx.mock
async def test_create_elicits_pixi_path(user_ctx):
    create = respx.post(clusters_url(K8S)).respond(201, json={"name": "cms.p"})

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="pixi")),
        accept(dask._PixiChoice(pixi_project="/depot/cms/alice/proj", pixi_env="ml")),
    )
    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](ctx)

    assert "cms.p" in out
    opts = json.loads(create.calls[0].request.content)["cluster_options"]
    assert opts["pixi_project"] == "/depot/cms/alice/proj"
    assert opts["pixi_env"] == "ml"
    assert len(ctx.calls) == 3


@respx.mock
async def test_create_elicits_conda_path_on_slurm(user_ctx):
    create = respx.post(clusters_url(SLURM)).respond(201, json={"name": "cms.c"})

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="slurm")),
        accept(dask._EnvChoice(env_source="conda")),
        accept(dask._CondaChoice(conda_env="/depot/cms/alice/envs/ana")),
    )
    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](ctx)

    assert "cms.c" in out
    opts = json.loads(create.calls[0].request.content)["cluster_options"]
    assert opts["conda_env"] == "/depot/cms/alice/envs/ana"


async def test_create_unsupported_client_returns_help(user_ctx):
    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](None)
    assert "needs two choices" in out


async def test_create_declined_cancels(user_ctx):
    ctx = FakeCtx(("decline", None))
    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](ctx)
    assert "cancelled" in out.lower()


# ── stop_dask_cluster ─────────────────────────────────────────────────────────


@respx.mock
async def test_stop_cluster(user_ctx):
    respx.delete(f"{K8S}/api/v1/clusters/c1").respond(204)

    tools = register_tools(dask).tools
    out = await tools["stop_dask_cluster"]("c1")
    assert "stopped" in out


@respx.mock
async def test_stop_cluster_already_gone(user_ctx):
    respx.delete(f"{K8S}/api/v1/clusters/c1").respond(404)

    tools = register_tools(dask).tools
    out = await tools["stop_dask_cluster"]("c1")
    assert "may have already stopped" in out


@respx.mock
async def test_stop_cluster_error_reported(user_ctx):
    respx.delete(f"{K8S}/api/v1/clusters/c1").respond(500, text="boom")

    tools = register_tools(dask).tools
    out = await tools["stop_dask_cluster"]("c1")
    assert "HTTP 500" in out


# ── get_dask_worker_count / get_dask_cluster_usage ─────────────────────────────


def test_cluster_id_strips_namespace_prefix():
    assert dask._cluster_id("cms.ec5c698a448943e9ae11ecfbdad5a6b0") == (
        "ec5c698a448943e9ae11ecfbdad5a6b0"
    )
    assert dask._cluster_id("ec5c698a") == "ec5c698a"


def prom_scalar(value):
    return {"data": {"result": [{"value": [0, str(value)]}]}}


def prom_vector(samples):
    return {
        "data": {
            "result": [
                {"metric": metric, "value": [0, str(value)]}
                for metric, value in samples
            ]
        }
    }


@respx.mock
async def test_worker_count_reports_total_and_states(user_ctx):
    respx.get(f"{K8S}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )

    def responder(request):
        import httpx

        q = request.url.params["query"]
        if "by (state)" in q:
            return httpx.Response(
                200,
                json=prom_vector([({"state": "idle"}, 2), ({"state": "saturated"}, 1)]),
            )
        if "desired_workers" in q:
            return httpx.Response(200, json=prom_scalar(3))
        return httpx.Response(200, json=prom_scalar(3))

    respx.get(f"{dask.PROMETHEUS_URL}/api/v1/query").mock(side_effect=responder)

    tools = register_tools(dask).tools
    out = await tools["get_dask_worker_count"]("cms.abc")

    assert "total: 3" in out
    assert "desired: 3" in out
    assert "idle=2" in out
    assert "saturated=1" in out


@respx.mock
async def test_worker_count_requires_ownership(user_ctx):
    respx.get(f"{K8S}/api/v1/clusters/cms.other").respond(404)

    tools = register_tools(dask).tools
    out = await tools["get_dask_worker_count"]("cms.other")
    assert "not found" in out


@respx.mock
async def test_cluster_usage_min_max_avg(user_ctx):
    respx.get(f"{K8S}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )

    def responder(request):
        import httpx

        q = request.url.params["query"]
        if "container_cpu_usage" in q:
            return httpx.Response(
                200,
                json=prom_vector(
                    [
                        ({"pod": "dask-worker-abc-1"}, 0.1),
                        ({"pod": "dask-worker-abc-2"}, 0.3),
                    ]
                ),
            )
        # memory
        return httpx.Response(
            200,
            json=prom_vector(
                [
                    ({"pod": "dask-worker-abc-1"}, 1 * 1024**3),
                    ({"pod": "dask-worker-abc-2"}, 3 * 1024**3),
                ]
            ),
        )

    respx.get(f"{dask.CLUSTER_PROMETHEUS_URL}/api/v1/query").mock(side_effect=responder)

    tools = register_tools(dask).tools
    out = await tools["get_dask_cluster_usage"]("cms.abc")

    assert "running workers sampled: 2" in out
    assert "min=0.100" in out and "max=0.300" in out and "avg=0.200" in out
    assert "min=1.00" in out and "max=3.00" in out and "avg=2.00" in out


@respx.mock
async def test_cluster_usage_no_workers(user_ctx):
    respx.get(f"{K8S}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )
    respx.get(f"{dask.CLUSTER_PROMETHEUS_URL}/api/v1/query").respond(
        200, json={"data": {"result": []}}
    )

    tools = register_tools(dask).tools
    out = await tools["get_dask_cluster_usage"]("cms.abc")
    assert "No Running worker pods" in out
