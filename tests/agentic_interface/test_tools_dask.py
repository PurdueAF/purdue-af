"""Tests for tools/dask.py — gateway resolution and cluster operations."""

import base64
import json

import pytest
import respx
from agentic_helpers import register_tools
from httpx import ConnectError
from tools import dask

K8S = dask._GATEWAYS["k8s"]
HAMMER = dask._GATEWAYS["slurm-hammer"]
GAUTSCHI = dask._GATEWAYS["slurm-gautschi"]
SLURM = dask._GATEWAYS["slurm"]


def clusters_url(base):
    return f"{base}/api/v1/clusters/"


def clusters_payload(*clusters):
    """Match Dask Gateway's GET /api/v1/clusters/ shape: {name: model, …}."""
    return {c["name"]: c for c in clusters}


def basic_alice():
    return "Basic " + base64.b64encode(b"alice:").decode()


# ── _resolve_gateway ──────────────────────────────────────────────────────────


def test_resolve_canonical_names():
    for name in ("k8s", "slurm-hammer", "slurm-gautschi", "slurm"):
        canonical, url = dask._resolve_gateway(name)
        assert canonical == name
        assert url == dask._GATEWAYS[name]


def test_resolve_aliases():
    assert dask._resolve_gateway("hammer")[0] == "slurm-hammer"
    assert dask._resolve_gateway("gautschi")[0] == "slurm-gautschi"


def test_resolve_is_case_insensitive():
    assert dask._resolve_gateway("K8S")[0] == "k8s"
    assert dask._resolve_gateway("Hammer")[0] == "slurm-hammer"


def test_resolve_unknown_raises():
    with pytest.raises(ValueError, match="Unknown gateway"):
        dask._resolve_gateway("nope")


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
    respx.get(clusters_url(HAMMER)).respond(403)  # unexpected auth failure
    respx.get(clusters_url(GAUTSCHI)).mock(side_effect=ConnectError("down"))
    respx.get(clusters_url(SLURM)).respond(200, json={})

    tools = register_tools(dask).tools
    out = await tools["list_dask_clusters"]()

    assert "# 1 Dask cluster(s)" in out
    assert "c-k8s" in out
    assert "slurm-hammer" not in out  # 403 suppressed
    assert "[slurm-gautschi] error: unreachable" in out


@respx.mock
async def test_list_clusters_all_empty(user_ctx):
    for base in (K8S, HAMMER, GAUTSCHI, SLURM):
        respx.get(clusters_url(base)).respond(200, json={})

    tools = register_tools(dask).tools
    assert await tools["list_dask_clusters"]() == (
        "No running Dask clusters on any gateway."
    )


@respx.mock
async def test_list_clusters_sends_basic_username(user_ctx):
    routes = [
        respx.get(clusters_url(base)).respond(200, json={})
        for base in (K8S, HAMMER, GAUTSCHI, SLURM)
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
    respx.post(f"{HAMMER}/api/v1/clusters/ghost/scale").respond(404)

    tools = register_tools(dask).tools
    out = await tools["scale_dask_cluster"]("ghost", 2, gateway="hammer")
    assert "not found on gateway 'hammer'" in out


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
