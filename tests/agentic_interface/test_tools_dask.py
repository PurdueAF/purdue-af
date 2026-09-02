"""Tests for tools/dask.py — gateway resolution and cluster operations."""

import base64
import json
import pathlib
import re

import httpx
import pytest
import respx
from agentic_helpers import failure, needs_choices, register_tools
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
    from errors import UserError

    with pytest.raises(UserError, match="Unknown gateway"):
        dask._resolve_gateway("nope")
    with pytest.raises(UserError, match="Unknown gateway"):
        dask._resolve_gateway("slurm-nonexistent")


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


# ── scale_dask_cluster ────────────────────────────────────────────────────────


@respx.mock
async def test_scale_posts_count(user_ctx):
    route = respx.post(f"{K8S}/api/v1/clusters/c1/scale").respond(200)

    tools = register_tools(dask).tools
    out = await tools["scale_dask_cluster"]("c1", 8)

    assert json.loads(route.calls.last.request.content) == {"count": 8}
    assert route.calls.last.request.headers["Authorization"] == basic_alice()
    assert "scaling to 8 worker(s)" in out


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
    from errors import UserError

    with pytest.raises(UserError, match="mutually exclusive"):
        dask._build_cluster_options(
            username="alice",
            pixi_project="/p",
            pixi_env="default",
            conda_env="/c",
            worker_cores=1,
            worker_memory=1,
            env=None,
        )

    with pytest.raises(UserError, match="provide either"):
        dask._build_cluster_options(
            username="alice",
            pixi_project=None,
            pixi_env="default",
            conda_env=None,
            worker_cores=1,
            worker_memory=1,
            env=None,
        )


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
        worker_cores=1,
        worker_memory=4,
        n_workers=0,
    )
    assert "slurm" in out
    assert "0 workers" in out
    assert create.called
    opts = json.loads(create.calls[0].request.content)["cluster_options"]
    assert opts["conda_env"] == "/depot/cms/alice/envs/ana"
    assert opts["pixi_project"] == ""


# ── create_dask_cluster: elicitation flows ────────────────────────────────────


def default_size():
    return accept(dask._SizeChoice(size="default"))


def count(value):
    return accept(dask._CountChoice(count=value))


@respx.mock
async def test_create_elicits_backend_and_global_env(user_ctx):
    create = respx.post(clusters_url(K8S)).respond(201, json={"name": "cms.g"})

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        default_size(),
        count("0"),
    )
    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](ctx)

    assert "cms.g" in out
    assert "0 workers" in out
    assert create.called
    opts = json.loads(create.calls[0].request.content)["cluster_options"]
    assert opts["pixi_project"] == dask.GLOBAL_PIXI_PROJECT
    assert opts["worker_cores"] == dask.DEFAULT_WORKER_CORES
    assert opts["worker_memory"] == dask.DEFAULT_WORKER_MEMORY
    assert len(ctx.calls) == 4


@respx.mock
async def test_create_global_on_slurm_rejected(user_ctx):
    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="slurm")),
        accept(dask._EnvChoice(env_source="global")),
    )
    tools = register_tools(dask).tools
    out = await failure(tools["create_dask_cluster"](ctx))

    assert "/work" in out
    assert "Slurm" in out


@respx.mock
async def test_create_elicits_pixi_path(user_ctx):
    create = respx.post(clusters_url(K8S)).respond(201, json={"name": "cms.p"})

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="pixi")),
        accept(dask._PixiChoice(pixi_project="/depot/cms/alice/proj", pixi_env="ml")),
        default_size(),
        count("0"),
    )
    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](ctx)

    assert "cms.p" in out
    opts = json.loads(create.calls[0].request.content)["cluster_options"]
    assert opts["pixi_project"] == "/depot/cms/alice/proj"
    assert opts["pixi_env"] == "ml"
    assert len(ctx.calls) == 5


@respx.mock
async def test_create_elicits_conda_path_on_slurm(user_ctx):
    create = respx.post(clusters_url(SLURM)).respond(201, json={"name": "cms.c"})

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="slurm")),
        accept(dask._EnvChoice(env_source="conda")),
        accept(dask._CondaChoice(conda_env="/depot/cms/alice/envs/ana")),
        default_size(),
        count("0"),
    )
    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](ctx)

    assert "cms.c" in out
    opts = json.loads(create.calls[0].request.content)["cluster_options"]
    assert opts["conda_env"] == "/depot/cms/alice/envs/ana"


@respx.mock
async def test_create_elicits_preset_count_scales(user_ctx):
    respx.post(clusters_url(K8S)).respond(201, json={"name": "cms.n"})
    scale = respx.post(f"{K8S}/api/v1/clusters/cms.n/scale").respond(204)

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        default_size(),
        count("50"),
    )
    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](ctx)

    assert "Scaling to 50" in out
    assert scale.called
    assert json.loads(scale.calls[0].request.content) == {"count": 50}


@respx.mock
async def test_create_elicits_custom_size_and_count(user_ctx):
    create = respx.post(clusters_url(K8S)).respond(201, json={"name": "cms.x"})
    scale = respx.post(f"{K8S}/api/v1/clusters/cms.x/scale").respond(204)

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        accept(dask._SizeChoice(size="custom")),
        accept(dask._CustomSize(worker_cores=8, worker_memory=16)),
        count("custom"),
        accept(dask._CustomCount(n_workers=25)),
    )
    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](ctx)

    assert "cms.x" in out
    opts = json.loads(create.calls[0].request.content)["cluster_options"]
    assert opts["worker_cores"] == 8
    assert opts["worker_memory"] == 16
    assert scale.called
    assert json.loads(scale.calls[0].request.content) == {"count": 25}
    assert len(ctx.calls) == 6


async def test_create_unsupported_client_returns_help(user_ctx):
    tools = register_tools(dask).tools
    out = await needs_choices(tools["create_dask_cluster"](None))
    assert "needs two choices" in out


async def test_create_declined_falls_back(user_ctx):
    # Agent clients may auto-decline elicitation without showing the user a
    # form — return the ask-in-chat guidance, not a dead-end "cancelled".
    ctx = FakeCtx(("decline", None))
    tools = register_tools(dask).tools
    out = await needs_choices(tools["create_dask_cluster"](ctx))
    assert "needs two choices" in out


async def test_create_declined_size_falls_back(user_ctx):
    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        ("cancel", None),
    )
    tools = register_tools(dask).tools
    out = await needs_choices(tools["create_dask_cluster"](ctx))
    assert "needs two choices" in out


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


# ── helper edge cases + remaining error paths ─────────────────────────────────


def test_parse_clusters_list_and_garbage():
    assert [c["name"] for c in dask._parse_clusters([{"name": "x"}, "skip"])] == ["x"]
    assert dask._parse_clusters("nope") == []


def test_stats_empty_and_base_env_skips_none():
    assert dask._stats([]) is None
    env = dask._base_worker_env("alice", {"KEEP": "1", "DROP": None})
    assert env["KEEP"] == "1"
    assert "DROP" not in env


def test_build_options_rejects_nonpositive_resources():
    from errors import UserError

    with pytest.raises(UserError, match="worker_cores"):
        dask._build_cluster_options(
            username="alice",
            pixi_project="/p",
            pixi_env="default",
            conda_env=None,
            worker_cores=0,
            worker_memory=1,
            env=None,
        )
    with pytest.raises(UserError, match="worker_memory"):
        dask._build_cluster_options(
            username="alice",
            pixi_project="/p",
            pixi_env="default",
            conda_env=None,
            worker_cores=1,
            worker_memory=0,
            env=None,
        )


@respx.mock
async def test_list_clusters_auth_and_http_errors(user_ctx):
    """401/403 are fetched but intentionally omitted from the list output;
    other HTTP errors are surfaced. Cover both via _fetch_clusters + list."""
    import httpx

    async with httpx.AsyncClient() as client:
        respx.get(clusters_url(K8S)).respond(403)
        gw, data = await dask._fetch_clusters(client, "k8s", K8S, "alice")
        assert data == "not authorised (no access to this backend)"

        respx.get(clusters_url(K8S)).respond(401)
        _, data = await dask._fetch_clusters(client, "k8s", K8S, "alice")
        assert "not authorised" in data

        respx.get(clusters_url(K8S)).respond(503)
        _, data = await dask._fetch_clusters(client, "k8s", K8S, "alice")
        assert data == "HTTP 503"

    def responder(request):
        if "k8s-slurm" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(403)

    respx.get(url__regex=r".*/api/v1/clusters/").mock(side_effect=responder)
    tools = register_tools(dask).tools
    out = await tools["list_dask_clusters"]()
    # auth errors suppressed; HTTP 503 from slurm is shown
    assert "not authorised" not in out
    assert "HTTP 503" in out


@respx.mock
async def test_require_owned_cluster_error_shapes(user_ctx):
    from errors import AuthError, UpstreamError

    respx.get(f"{K8S}/api/v1/clusters/c1").mock(side_effect=ConnectError("down"))
    with pytest.raises(UpstreamError, match="unreachable"):
        await dask._require_owned_cluster(K8S, "alice", "c1", "k8s")

    respx.get(f"{K8S}/api/v1/clusters/c1").respond(403)
    with pytest.raises(AuthError, match="not authorised"):
        await dask._require_owned_cluster(K8S, "alice", "c1", "k8s")

    respx.get(f"{K8S}/api/v1/clusters/c1").respond(500, text="boom")
    with pytest.raises(UpstreamError, match="HTTP 500"):
        await dask._require_owned_cluster(K8S, "alice", "c1", "k8s")

    respx.get(f"{K8S}/api/v1/clusters/c1").respond(200, json={"name": "c1"})
    assert await dask._require_owned_cluster(K8S, "alice", "c1", "k8s") is None


@respx.mock
async def test_prom_helpers_report_problems_separately_from_no_data():
    import httpx

    async with httpx.AsyncClient() as client:
        respx.get(f"{dask.PROMETHEUS_URL}/api/v1/query").mock(
            side_effect=ConnectError("down")
        )
        value, problem = await dask._prom_scalar(client, dask.PROMETHEUS_URL, "up")
        assert value is None and problem.startswith("is unreachable")
        rows, problem = await dask._prom_vector(client, dask.PROMETHEUS_URL, "up")
        assert rows == [] and problem.startswith("is unreachable")

        respx.get(f"{dask.PROMETHEUS_URL}/api/v1/query").respond(500)
        value, problem = await dask._prom_scalar(client, dask.PROMETHEUS_URL, "up")
        assert value is None and problem == "returned HTTP 500"
        rows, problem = await dask._prom_vector(client, dask.PROMETHEUS_URL, "up")
        assert rows == [] and problem == "returned HTTP 500"

        respx.get(f"{dask.PROMETHEUS_URL}/api/v1/query").respond(
            200, json={"data": {"result": []}}
        )
        assert await dask._prom_scalar(client, dask.PROMETHEUS_URL, "up") == (
            None,
            None,
        )

        respx.get(f"{dask.PROMETHEUS_URL}/api/v1/query").respond(
            200, json={"data": {"result": [{"value": [0, "bad"]}]}}
        )
        assert await dask._prom_scalar(client, dask.PROMETHEUS_URL, "up") == (
            None,
            None,
        )

        respx.get(f"{dask.PROMETHEUS_URL}/api/v1/query").respond(
            200,
            json={
                "data": {
                    "result": [
                        {"metric": {"a": "1"}, "value": [0, "1.5"]},
                        {"metric": {}, "value": [0, "x"]},
                    ]
                }
            },
        )
        rows, problem = await dask._prom_vector(client, dask.PROMETHEUS_URL, "up")
        assert rows == [({"a": "1"}, 1.5)]
        assert problem is None


async def test_create_cancel_and_unsupported_mid_flow(user_ctx):
    tools = register_tools(dask).tools

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        ("cancel", None),
    )
    assert "needs two choices" in await needs_choices(tools["create_dask_cluster"](ctx))

    class BoomCtx(FakeCtx):
        async def elicit(self, message, schema):
            if not self._responses:
                raise RuntimeError("elicit broken")
            return await super().elicit(message, schema)

    # backend accepted, then env elicit fails → help text
    ctx = BoomCtx(accept(dask._BackendChoice(gateway="k8s")))
    out = await needs_choices(tools["create_dask_cluster"](ctx))
    assert "needs two choices" in out


@respx.mock
async def test_create_gateway_errors_and_scale_failures(user_ctx):
    tools = register_tools(dask).tools
    kwargs = dict(
        gateway="k8s",
        pixi_project="/work/alice/p",
        worker_cores=1,
        worker_memory=4,
        n_workers=2,
    )

    respx.post(clusters_url(K8S)).mock(side_effect=ConnectError("down"))
    assert "unreachable" in await failure(
        tools["create_dask_cluster"](FakeCtx(), **kwargs)
    )

    respx.post(clusters_url(K8S)).respond(500, text="boom")
    assert "HTTP 500" in await failure(
        tools["create_dask_cluster"](FakeCtx(), **kwargs)
    )

    respx.post(clusters_url(K8S)).respond(201, json={})
    assert "not a cluster record with a name" in await failure(
        tools["create_dask_cluster"](FakeCtx(), **{**kwargs, "n_workers": 0})
    )

    respx.post(clusters_url(K8S)).respond(201, json={"name": "cms.s"})
    respx.post(f"{K8S}/api/v1/clusters/cms.s/scale").mock(
        side_effect=ConnectError("down")
    )
    out = await tools["create_dask_cluster"](FakeCtx(), **kwargs)
    assert "Created with 0 workers — the scale request failed" in out
    assert "unreachable" in out
    assert "scale_dask_cluster('cms.s', 2, gateway='k8s')" in out

    respx.post(clusters_url(K8S)).respond(201, json={"name": "cms.s2"})
    respx.post(f"{K8S}/api/v1/clusters/cms.s2/scale").respond(500, text="no")
    out = await tools["create_dask_cluster"](FakeCtx(), **kwargs)
    assert "returned HTTP 500 while trying to scale to 2 worker(s)" in out


@respx.mock
async def test_worker_count_no_metrics_and_bad_gateway(user_ctx):
    tools = register_tools(dask).tools
    assert "Unknown gateway" in await failure(
        tools["get_dask_worker_count"]("c1", gateway="x")
    )

    respx.get(f"{K8S}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )
    respx.get(f"{dask.PROMETHEUS_URL}/api/v1/query").respond(
        200, json={"data": {"result": []}}
    )
    out = await tools["get_dask_worker_count"]("cms.abc")
    assert "No worker metrics" in out


@respx.mock
async def test_usage_partial_metrics_and_errors(user_ctx):
    tools = register_tools(dask).tools
    assert "Unknown gateway" in await failure(
        tools["get_dask_cluster_usage"]("c1", gateway="x")
    )

    respx.get(f"{K8S}/api/v1/clusters/cms.abc").respond(403)
    assert "not authorised" in await failure(tools["get_dask_cluster_usage"]("cms.abc"))

    respx.get(f"{K8S}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )

    def responder(request):
        import httpx

        q = request.url.params["query"]
        if "container_cpu_usage" in q:
            return httpx.Response(200, json=prom_vector([({"pod": "w1"}, 0.5)]))
        return httpx.Response(200, json={"data": {"result": []}})

    respx.get(f"{dask.CLUSTER_PROMETHEUS_URL}/api/v1/query").mock(side_effect=responder)
    out = await tools["get_dask_cluster_usage"]("cms.abc")
    assert "CPU (cores):" in out
    assert "Memory (GiB): no data" in out


@respx.mock
async def test_create_elicits_unsupported_on_size_and_count(user_ctx):
    """Mid-flow elicit failures after env is chosen return the help text."""
    tools = register_tools(dask).tools

    class BoomAfter(FakeCtx):
        async def elicit(self, message, schema):
            if "worker size" in message or "How many workers" in message:
                raise RuntimeError("boom")
            return await super().elicit(message, schema)

    ctx = BoomAfter(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
    )
    assert "needs two choices" in await needs_choices(tools["create_dask_cluster"](ctx))

    class BoomCustom(FakeCtx):
        async def elicit(self, message, schema):
            if "resources per worker" in message or "number of workers" in message:
                raise RuntimeError("boom")
            return await super().elicit(message, schema)

    ctx = BoomCustom(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        accept(dask._SizeChoice(size="custom")),
    )
    assert "needs two choices" in await needs_choices(tools["create_dask_cluster"](ctx))

    ctx = BoomCustom(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        accept(dask._SizeChoice(size="default")),
        count("custom"),
    )
    assert "needs two choices" in await needs_choices(tools["create_dask_cluster"](ctx))


async def test_create_cancel_on_pixi_conda_custom(user_ctx):
    tools = register_tools(dask).tools

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="pixi")),
        ("cancel", None),
    )
    assert "needs two choices" in await needs_choices(tools["create_dask_cluster"](ctx))

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="conda")),
        ("cancel", None),
    )
    assert "needs two choices" in await needs_choices(tools["create_dask_cluster"](ctx))

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        accept(dask._SizeChoice(size="custom")),
        ("cancel", None),
    )
    assert "needs two choices" in await needs_choices(tools["create_dask_cluster"](ctx))

    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        accept(dask._SizeChoice(size="default")),
        count("custom"),
        ("cancel", None),
    )
    assert "needs two choices" in await needs_choices(tools["create_dask_cluster"](ctx))


async def test_create_unsupported_on_pixi_conda_and_count(user_ctx):
    tools = register_tools(dask).tools

    class BoomPath(FakeCtx):
        async def elicit(self, message, schema):
            if "pixi project" in message or "conda environment" in message:
                raise RuntimeError("boom")
            if "How many workers" in message:
                raise RuntimeError("boom")
            return await super().elicit(message, schema)

    assert "needs two choices" in await needs_choices(
        tools["create_dask_cluster"](
            BoomPath(
                accept(dask._BackendChoice(gateway="k8s")),
                accept(dask._EnvChoice(env_source="pixi")),
            )
        )
    )
    assert "needs two choices" in await needs_choices(
        tools["create_dask_cluster"](
            BoomPath(
                accept(dask._BackendChoice(gateway="k8s")),
                accept(dask._EnvChoice(env_source="conda")),
            )
        )
    )
    assert "needs two choices" in await needs_choices(
        tools["create_dask_cluster"](
            BoomPath(
                accept(dask._BackendChoice(gateway="k8s")),
                accept(dask._EnvChoice(env_source="global")),
                accept(dask._SizeChoice(size="default")),
            )
        )
    )


async def test_create_cancel_on_worker_count_prompt(user_ctx):
    tools = register_tools(dask).tools
    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        accept(dask._SizeChoice(size="default")),
        ("cancel", None),
    )
    assert "needs two choices" in await needs_choices(tools["create_dask_cluster"](ctx))


@respx.mock
async def test_create_returns_build_options_error(user_ctx, monkeypatch):
    """If _build_cluster_options rejects after elicitation, surface that failure."""
    from errors import UserError

    tools = register_tools(dask).tools

    def reject(**kw):
        raise UserError("Error: synthetic options failure")

    monkeypatch.setattr(dask, "_build_cluster_options", reject)
    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        accept(dask._SizeChoice(size="default")),
        count("0"),
    )
    out = await failure(tools["create_dask_cluster"](ctx))
    assert "synthetic options failure" in out


@respx.mock
async def test_usage_cpu_missing_memory_present(user_ctx):
    respx.get(f"{K8S}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )

    def responder(request):
        import httpx

        q = request.url.params["query"]
        if "container_cpu_usage" in q:
            return httpx.Response(200, json={"data": {"result": []}})
        return httpx.Response(200, json=prom_vector([({"pod": "w1"}, 2 * 1024**3)]))

    respx.get(f"{dask.CLUSTER_PROMETHEUS_URL}/api/v1/query").mock(side_effect=responder)
    tools = register_tools(dask).tools
    out = await tools["get_dask_cluster_usage"]("cms.abc")
    assert "CPU (cores): no data" in out
    assert "Memory (GiB):" in out


# ── hardening: cluster-name validation, worker caps, PromQL escaping ──────────


@pytest.mark.parametrize(
    "bad",
    [
        "c1/evil",  # path traversal into the gateway URL
        "../clusters",  # dotdot segment
        "cms.abc(.+)",  # regex metachars would widen the PromQL pod match
        'a"b',  # quote would break out of a label matcher
        "a b",
        "",
    ],
)
@respx.mock
async def test_malformed_cluster_name_rejected_everywhere(user_ctx, bad):
    """Every cluster_name-taking tool rejects unsafe names before any request."""
    tools = register_tools(dask).tools
    for out in [
        await failure(tools["get_dask_cluster_info"](bad)),
        await failure(tools["get_dask_worker_count"](bad)),
        await failure(tools["get_dask_cluster_usage"](bad)),
        await failure(tools["scale_dask_cluster"](bad, 1)),
        await failure(tools["stop_dask_cluster"](bad)),
    ]:
        assert "invalid cluster name" in out
        assert "list_dask_clusters" in out
    assert not respx.calls  # rejected before anything left the process


@respx.mock
async def test_create_rejects_over_cap_explicit_and_elicited(user_ctx):
    tools = register_tools(dask).tools
    out = await failure(
        tools["create_dask_cluster"](FakeCtx(), n_workers=dask.MAX_WORKERS + 1)
    )
    assert f"≤ {dask.MAX_WORKERS}" in out

    # elicited custom-count path is capped too
    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        default_size(),
        count("custom"),
        accept(dask._CustomCount(n_workers=dask.MAX_WORKERS + 1)),
    )
    out = await failure(tools["create_dask_cluster"](ctx))
    assert f"≤ {dask.MAX_WORKERS}" in out
    assert not respx.calls


@respx.mock
@pytest.mark.parametrize(
    ("gateway", "cores", "memory", "fragment"),
    [
        ("k8s", 65, 4, "worker_cores must be between 0.1 and 64"),
        ("k8s", 0.05, 4, "worker_cores must be between 0.1 and 64"),
        ("k8s", 1, 65, "worker_memory must be between 0.1 and 64"),
        ("slurm", 17, 4, "worker_cores must be between 1 and 16"),
        ("slurm", 1.5, 4, "whole number of worker_cores"),
        ("slurm", 1, 0.5, "worker_memory must be between 1 and 64"),
    ],
)
async def test_create_rejects_size_beyond_gateway_limits(
    user_ctx, gateway, cores, memory, fragment
):
    """Per-worker size caps mirror the gateway configs (see _WORKER_LIMITS)."""
    tools = register_tools(dask).tools
    out = await failure(
        tools["create_dask_cluster"](
            FakeCtx(),
            gateway=gateway,
            conda_env="/depot/cms/alice/env",
            worker_cores=cores,
            worker_memory=memory,
            n_workers=0,
        )
    )
    assert fragment in out
    assert not respx.calls  # rejected before anything left the process


@respx.mock
async def test_create_rejects_elicited_size_beyond_limits(user_ctx):
    """The elicited custom-size path goes through the same gateway limits."""
    tools = register_tools(dask).tools
    ctx = FakeCtx(
        accept(dask._BackendChoice(gateway="k8s")),
        accept(dask._EnvChoice(env_source="global")),
        accept(dask._SizeChoice(size="custom")),
        accept(dask._CustomSize(worker_cores=128, worker_memory=4)),
        count("0"),
    )
    out = await failure(tools["create_dask_cluster"](ctx))
    assert "worker_cores must be between 0.1 and 64" in out
    assert not respx.calls


def test_max_workers_matches_gateway_config():
    """MAX_WORKERS mirrors cluster_max_workers in the k8s gateway values."""
    values = (
        pathlib.Path(__file__).resolve().parents[2]
        / "apps/dask-gateway/dask-gateway-k8s/values.yaml"
    ).read_text()
    m = re.search(r"cluster_max_workers\s*=\s*(\d+)", values)
    assert m, "cluster_max_workers not found in gateway values"
    assert dask.MAX_WORKERS == int(m.group(1))


@respx.mock
async def test_worker_count_escapes_username_in_promql():
    """A username with quotes cannot break out of the PromQL label matcher."""
    from context import current_user
    from shared import quote_label

    hostile = 'ali"} or {job!="'
    token = current_user.set({"username": hostile, "namespace": "cms", "token": "t"})
    try:
        respx.get(f"{K8S}/api/v1/clusters/cms.abc").respond(
            200, json={"name": "cms.abc", "status": "RUNNING"}
        )
        seen = []

        def responder(request):
            import httpx

            seen.append(request.url.params["query"])
            return httpx.Response(200, json=prom_scalar(1))

        respx.get(f"{dask.PROMETHEUS_URL}/api/v1/query").mock(side_effect=responder)

        tools = register_tools(dask).tools
        await tools["get_dask_worker_count"]("cms.abc")
    finally:
        current_user.reset(token)

    assert seen
    for q in seen:
        assert f'user="{hostile}"' not in q  # raw interpolation would inject
        assert quote_label(hostile) in q


@respx.mock
async def test_usage_regex_escapes_cluster_id(user_ctx):
    """The pod regex uses re.escape on the cluster id (defense in depth)."""
    respx.get(f"{K8S}/api/v1/clusters/abc-1").respond(
        200, json={"name": "abc-1", "status": "RUNNING"}
    )
    seen = []

    def responder(request):
        import httpx

        seen.append(request.url.params["query"])
        return httpx.Response(200, json={"data": {"result": []}})

    respx.get(f"{dask.CLUSTER_PROMETHEUS_URL}/api/v1/query").mock(side_effect=responder)
    tools = register_tools(dask).tools
    await tools["get_dask_cluster_usage"]("abc-1")

    assert seen
    for q in seen:
        assert 'pod=~"dask-worker-abc\\-1-.+"' in q


# ── failure translation ───────────────────────────────────────────────────────


@respx.mock
async def test_list_clusters_all_gateways_refused_is_an_error(user_ctx):
    respx.get(url__regex=r".*/api/v1/clusters/").respond(403)
    out = await failure(register_tools(dask).tools["list_dask_clusters"]())
    assert out.startswith("Error: not authorised on any gateway")
    assert "No running Dask clusters" not in out


@respx.mock
async def test_list_clusters_notes_a_refused_gateway_when_nothing_is_listed(user_ctx):
    import httpx

    def responder(request):
        if "k8s-slurm" in str(request.url):
            return httpx.Response(403)
        return httpx.Response(200, json={})

    respx.get(url__regex=r".*/api/v1/clusters/").mock(side_effect=responder)
    out = await register_tools(dask).tools["list_dask_clusters"]()
    assert out.startswith(
        "No running Dask clusters on any gateway (gateway slurm: not authorised"
    )


@respx.mock
async def test_list_clusters_reports_the_gateways_reason(user_ctx):
    import httpx

    def responder(request):
        if "k8s-slurm" in str(request.url):
            return httpx.Response(503, json={"message": "scheduler restarting"})
        return httpx.Response(200, json={})

    respx.get(url__regex=r".*/api/v1/clusters/").mock(side_effect=responder)
    out = await register_tools(dask).tools["list_dask_clusters"]()
    assert "[slurm] error: HTTP 503 — scheduler restarting" in out


@respx.mock
async def test_worker_count_prometheus_down_is_not_no_metrics(user_ctx):
    respx.get(f"{K8S}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )
    respx.get(f"{dask.PROMETHEUS_URL}/api/v1/query").mock(
        side_effect=ConnectError("down")
    )
    out = await failure(register_tools(dask).tools["get_dask_worker_count"]("cms.abc"))
    assert out.startswith(
        "Error: could not read worker metrics for 'cms.abc' — Prometheus is unreachable"
    )
    assert "No worker metrics" not in out
    assert "get_dask_cluster_info" in out


@respx.mock
async def test_usage_monitoring_down_is_reported(user_ctx):
    respx.get(f"{K8S}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )
    respx.get(f"{dask.CLUSTER_PROMETHEUS_URL}/api/v1/query").respond(500, text="boom")
    out = await failure(register_tools(dask).tools["get_dask_cluster_usage"]("cms.abc"))
    assert out.startswith(
        "Error: could not read resource usage for 'cms.abc' — the monitoring "
        "system returned HTTP 500 — boom"
    )


# ── failure translation, tabulated ────────────────────────────────────────────
#
# Every gateway-facing tool goes through the same helper, so one table covers
# the shapes that matter: what the gateway answered, which Failure class the
# user gets, and the fragments of the message that carry the diagnosis.

CREATE = dict(
    gateway="k8s", pixi_project="/p", worker_cores=1, worker_memory=1, n_workers=0
)
INFO, SCALE, OPTIONS, CREATE_URL = (
    f"{K8S}/api/v1/clusters/c1",
    f"{K8S}/api/v1/clusters/c1/scale",
    f"{K8S}/api/v1/options",
    clusters_url(K8S),
)
DOWN = ConnectError("down")


def _resp(status, **kwargs):
    return httpx.Response(status, **kwargs)


# fmt: off
GATEWAY_FAILURES = [
    # tool, arguments, method, url, gateway answer, Failure class, message fragments
    ("get_dask_cluster_info", {"cluster_name": "c1"}, "GET", INFO, DOWN, "UpstreamError", ["gateway 'k8s' unreachable — connection failed (down)"]),
    ("get_dask_cluster_info", {"cluster_name": "c1"}, "GET", INFO, _resp(500, text="err"), "UpstreamError", ["HTTP 500"]),
    ("get_dask_cluster_info", {"cluster_name": "c1"}, "GET", INFO, _resp(502, text="<h1>Bad Gateway</h1>"), "UpstreamError", ["returned HTTP 502 while trying to inspect cluster 'c1' (it is down or restarting behind its proxy) — Bad Gateway"]),
    ("get_dask_cluster_info", {"cluster_name": "c1"}, "GET", INFO, _resp(404), "UserError", ["Cluster 'c1' not found on gateway 'k8s'.", "list_dask_clusters"]),
    ("get_dask_cluster_info", {"cluster_name": "c1", "gateway": "slurm"}, "GET", f"{SLURM}/api/v1/clusters/c1", _resp(403), "AuthError", ["Error: not authorised on gateway 'slurm' to inspect cluster 'c1' (HTTP 403).", "Hammer"]),
    ("get_dask_cluster_info", {"cluster_name": "c1"}, "GET", INFO, _resp(200, text="nope"), "UpstreamError", ["not a cluster record"]),
    ("get_dask_worker_count", {"cluster_name": "c1"}, "GET", INFO, _resp(404), "UserError", ["Cluster 'c1' not found on gateway 'k8s'"]),
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": 3}, "POST", SCALE, DOWN, "UpstreamError", ["unreachable"]),
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": 3}, "POST", SCALE, _resp(500, text="no"), "UpstreamError", ["HTTP 500"]),
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": 3, "gateway": "slurm"}, "POST", f"{SLURM}/api/v1/clusters/c1/scale", _resp(404), "UserError", ["Cluster 'c1' not found on gateway 'slurm'.", "list_dask_clusters"]),
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": 3}, "POST", SCALE, _resp(409, json={"message": "cluster is stopping"}), "UserError", ["Error: gateway 'k8s' rejected the request to scale to 3 worker(s) — cluster is stopping."]),
    ("stop_dask_cluster", {"cluster_name": "c1"}, "DELETE", INFO, DOWN, "UpstreamError", ["unreachable"]),
    ("stop_dask_cluster", {"cluster_name": "c1"}, "DELETE", INFO, _resp(500, text="boom"), "UpstreamError", ["HTTP 500", "boom"]),
    ("list_dask_cluster_options", {}, "GET", OPTIONS, DOWN, "UpstreamError", ["unreachable"]),
    ("list_dask_cluster_options", {}, "GET", OPTIONS, _resp(401), "AuthError", ["not authorised on gateway 'k8s' to list cluster options (HTTP 401)"]),
    ("list_dask_cluster_options", {}, "GET", OPTIONS, _resp(500, text="nope"), "UpstreamError", ["HTTP 500"]),
    ("list_dask_cluster_options", {}, "GET", OPTIONS, _resp(200, text="nope"), "UpstreamError", ["not a cluster-options document"]),
    ("create_dask_cluster", CREATE, "POST", CREATE_URL, DOWN, "UpstreamError", ["gateway 'k8s' unreachable"]),
    ("create_dask_cluster", CREATE, "POST", CREATE_URL, _resp(500, text="boom"), "UpstreamError", ["HTTP 500"]),
    ("create_dask_cluster", CREATE, "POST", CREATE_URL, _resp(422, json={"message": "User already has 1 active clusters"}), "UserError", ["rejected", "active clusters"]),
    ("create_dask_cluster", CREATE, "POST", CREATE_URL, _resp(422, text="plain reject"), "UserError", ["rejected", "plain reject"]),
    ("create_dask_cluster", CREATE, "POST", CREATE_URL, _resp(201, text="<html>proxy</html>"), "UpstreamError", ["not a cluster record with a name", "list_dask_clusters before creating another"]),
    ("create_dask_cluster", CREATE, "POST", CREATE_URL, _resp(201, json={}), "UpstreamError", ["not a cluster record with a name"]),
]

REJECTED_ARGUMENTS = [
    # tool, arguments, message fragment — refused before anything leaves the process
    ("get_dask_cluster_info", {"cluster_name": "c1", "gateway": "bogus"}, "Unknown gateway"),
    ("list_dask_cluster_options", {"gateway": "bogus"}, "Unknown gateway"),
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": 1, "gateway": "x"}, "Unknown gateway"),
    ("stop_dask_cluster", {"cluster_name": "c1", "gateway": "x"}, "Unknown gateway"),
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": -1}, "must be ≥ 0"),
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": dask.MAX_WORKERS + 1}, f"≤ {dask.MAX_WORKERS}"),
    ("create_dask_cluster", {"n_workers": -1}, "n_workers"),
    ("create_dask_cluster", {"n_workers": dask.MAX_WORKERS + 1}, f"≤ {dask.MAX_WORKERS}"),
    ("create_dask_cluster", {"worker_cores": 0, "pixi_project": "/p", "gateway": "k8s"}, "worker_cores"),
    ("create_dask_cluster", {"worker_memory": -1, "pixi_project": "/p", "gateway": "k8s"}, "worker_memory"),
    ("create_dask_cluster", {**CREATE, "gateway": "bogus"}, "Unknown gateway"),
    ("create_dask_cluster", {**CREATE, "pixi_project": None, "env_source": "weird"}, "unknown env_source"),
]
# fmt: on


def _call(tools, tool, arguments):
    if tool == "create_dask_cluster":
        return tools[tool](FakeCtx(), **arguments)
    return tools[tool](**arguments)


@pytest.mark.parametrize(
    ("tool", "arguments", "method", "url", "answer", "cls", "fragments"),
    GATEWAY_FAILURES,
    ids=[f"{t[0]}-{i}" for i, t in enumerate(GATEWAY_FAILURES)],
)
@respx.mock
async def test_gateway_answers_are_translated(
    user_ctx, tool, arguments, method, url, answer, cls, fragments
):
    import errors

    route = respx.request(method, url)
    if isinstance(answer, Exception):
        route.mock(side_effect=answer)
    else:
        route.mock(return_value=answer)
    with pytest.raises(getattr(errors, cls)) as info:
        await _call(register_tools(dask).tools, tool, arguments)
    for fragment in fragments:
        assert fragment in str(info.value), (fragment, str(info.value))


@pytest.mark.parametrize(
    ("tool", "arguments", "fragment"),
    REJECTED_ARGUMENTS,
    ids=[f"{t[0]}-{t[2]}" for t in REJECTED_ARGUMENTS],
)
@respx.mock
async def test_bad_arguments_are_refused_before_any_request(
    user_ctx, tool, arguments, fragment
):
    from errors import UserError

    with pytest.raises(UserError, match=re.escape(fragment)):
        await _call(register_tools(dask).tools, tool, arguments)
    assert not respx.calls
