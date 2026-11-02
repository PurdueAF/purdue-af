"""Tests for tools/dask.py — cluster operations against Dask Gateway."""

import json
import re

import httpx
import pytest
import respx
from agentic_helpers import FakeCtx, as_user, failure, needs_choices, register_tools
from httpx import ConnectError
from mcp.shared.exceptions import NoBackChannelError
from tools import dask

GATEWAY = dask.DASK_GATEWAY_URL
CLUSTERS = f"{GATEWAY}/api/v1/clusters/"


def clusters_payload(*clusters):
    """Match Dask Gateway's GET /api/v1/clusters/ shape: {name: model, …}."""
    return {c["name"]: c for c in clusters}


def alice_token():
    return "jupyterhub tok-alice"


def scheduler_running(name):
    """Mock the status GET that create/scale use to wait for the scheduler."""
    return respx.get(f"{GATEWAY}/api/v1/clusters/{name}").respond(
        200, json={"name": name, "status": "RUNNING"}
    )


def accept(data):
    return ("accept", data)


# ── _fmt_cluster / _parse_clusters ────────────────────────────────────────────


def test_fmt_cluster_fixed_workers():
    out = dask._fmt_cluster(
        {"name": "c1", "status": "RUNNING", "workers": {"w1": {}, "w2": {}}}
    )
    assert "workers=2" in out


def test_fmt_cluster_adaptive():
    out = dask._fmt_cluster(
        {"name": "c1", "status": "RUNNING", "adaptive": {"minimum": 1, "maximum": 10}}
    )
    assert "adaptive(1–10)" in out


def test_fmt_cluster_scheduler_address():
    out = dask._fmt_cluster(
        {"name": "c1", "status": "RUNNING", "scheduler_address": "tls://x:8786"}
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
async def test_list_clusters_renders_each_cluster(user_ctx):
    respx.get(CLUSTERS).respond(
        200,
        json=clusters_payload(
            {"name": "cms.a", "status": "RUNNING", "workers": {}},
            {"name": "cms.b", "status": "PENDING", "workers": {}},
        ),
    )

    tools = register_tools(dask).tools
    out = await tools["list_dask_clusters"]()

    assert "# 2 Dask cluster(s)" in out
    assert "cms.a" in out and "cms.b" in out


@respx.mock
async def test_list_clusters_empty(user_ctx):
    respx.get(CLUSTERS).respond(200, json={})

    tools = register_tools(dask).tools
    assert await tools["list_dask_clusters"]() == "No running Dask clusters."


@respx.mock
async def test_list_clusters_sends_the_callers_token(user_ctx):
    route = respx.get(CLUSTERS).respond(200, json={})

    tools = register_tools(dask).tools
    await tools["list_dask_clusters"]()

    assert route.calls.last.request.headers["Authorization"] == alice_token()


@respx.mock
async def test_gateway_session_cookie_is_not_replayed_for_the_next_caller(user_ctx):
    route = respx.get(CLUSTERS).respond(
        200, json={}, headers={"Set-Cookie": "dask-gateway-abc=alice; Path=/"}
    )
    tools = register_tools(dask).tools
    await tools["list_dask_clusters"]()

    with as_user(username="bob", token="tok-bob"):
        await tools["list_dask_clusters"]()

    request = route.calls.last.request
    assert request.headers["Authorization"] == "jupyterhub tok-bob"
    assert "cookie" not in request.headers


# ── get_dask_cluster_info ─────────────────────────────────────────────────────


@respx.mock
async def test_cluster_info_renders_details(user_ctx):
    workers = {f"w{i}": {"status": "running"} for i in range(25)}
    respx.get(f"{GATEWAY}/api/v1/clusters/c1").respond(
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
    scheduler_running("c1")
    route = respx.post(f"{GATEWAY}/api/v1/clusters/c1/scale").respond(200)

    tools = register_tools(dask).tools
    out = await tools["scale_dask_cluster"]("c1", 8)

    assert json.loads(route.calls.last.request.content) == {"count": 8}
    assert route.calls.last.request.headers["Authorization"] == alice_token()
    assert "scaling to 8 worker(s)" in out


# ── waiting for the scheduler before scaling ──────────────────────────────────
#
# A freshly created cluster is PENDING until its scheduler pod is up, and the
# gateway refuses to scale a cluster that is not RUNNING. create/scale therefore
# poll the cluster record first; these tests pin that they wait rather than fail.


def pending_then_running(name, pending=2):
    """Status GET that reports PENDING `pending` times, then RUNNING."""
    seen = {"n": 0}

    def responder(request):
        seen["n"] += 1
        status = "PENDING" if seen["n"] <= pending else "RUNNING"
        return httpx.Response(200, json={"name": name, "status": status})

    respx.get(f"{GATEWAY}/api/v1/clusters/{name}").mock(side_effect=responder)
    return seen


@respx.mock
async def test_scale_waits_for_a_pending_scheduler(user_ctx, monkeypatch):
    monkeypatch.setattr(dask, "SCHEDULER_POLL_INTERVAL", 0)
    polls = pending_then_running("c1", pending=2)
    scale = respx.post(f"{GATEWAY}/api/v1/clusters/c1/scale").respond(204)

    out = await register_tools(dask).tools["scale_dask_cluster"]("c1", 4)

    assert polls["n"] == 3  # PENDING, PENDING, RUNNING
    assert scale.called
    assert json.loads(scale.calls[0].request.content) == {"count": 4}
    assert "scaling to 4 worker(s)" in out


@respx.mock
async def test_scale_does_not_post_when_the_scheduler_never_comes_up(
    user_ctx, monkeypatch
):
    monkeypatch.setattr(dask, "SCHEDULER_POLL_INTERVAL", 0)
    monkeypatch.setattr(dask, "SCHEDULER_READY_TIMEOUT", 0.01)
    respx.get(f"{GATEWAY}/api/v1/clusters/c1").respond(
        200, json={"name": "c1", "status": "PENDING"}
    )
    scale = respx.post(f"{GATEWAY}/api/v1/clusters/c1/scale").respond(204)

    out = await register_tools(dask).tools["scale_dask_cluster"]("c1", 4)

    assert not scale.called
    assert "No scale request was sent" in out
    assert "still PENDING" in out
    assert "query_dask_logs" in out


@pytest.mark.parametrize("status", ["STOPPED", "FAILED", "STOPPING"])
@respx.mock
async def test_scale_refuses_a_cluster_that_will_never_run(user_ctx, status):
    respx.get(f"{GATEWAY}/api/v1/clusters/c1").respond(
        200, json={"name": "c1", "status": status}
    )
    scale = respx.post(f"{GATEWAY}/api/v1/clusters/c1/scale").respond(204)

    message = await failure(register_tools(dask).tools["scale_dask_cluster"]("c1", 4))

    assert not scale.called
    assert status in message
    assert "will not accept workers" in message


@respx.mock
async def test_create_waits_for_the_scheduler_before_scaling(user_ctx, monkeypatch):
    """create scales only once the scheduler is RUNNING."""
    monkeypatch.setattr(dask, "SCHEDULER_POLL_INTERVAL", 0)
    respx.post(CLUSTERS).respond(201, json={"name": "cms.slow"})
    polls = pending_then_running("cms.slow", pending=3)
    scale = respx.post(f"{GATEWAY}/api/v1/clusters/cms.slow/scale").respond(204)

    out = await register_tools(dask).tools["create_dask_cluster"](
        FakeCtx(),
        env_source="global",
        worker_cores=1,
        worker_memory=4,
        n_workers=2,
    )

    assert polls["n"] == 4
    assert scale.called
    assert json.loads(scale.calls[0].request.content) == {"count": 2}
    assert "Scaling to 2 worker(s)." in out
    # the count is a Prometheus scrape, so say it lags before the caller looks
    assert "trail reality" in out


@respx.mock
async def test_create_reports_a_scheduler_that_never_came_up(user_ctx, monkeypatch):
    monkeypatch.setattr(dask, "SCHEDULER_POLL_INTERVAL", 0)
    monkeypatch.setattr(dask, "SCHEDULER_READY_TIMEOUT", 0.01)
    respx.post(CLUSTERS).respond(201, json={"name": "cms.stuck"})
    respx.get(f"{GATEWAY}/api/v1/clusters/cms.stuck").respond(
        200, json={"name": "cms.stuck", "status": "PENDING"}
    )
    scale = respx.post(f"{GATEWAY}/api/v1/clusters/cms.stuck/scale").respond(204)

    out = await register_tools(dask).tools["create_dask_cluster"](
        FakeCtx(),
        env_source="global",
        worker_cores=1,
        worker_memory=4,
        n_workers=2,
    )

    # the cluster exists, so this is a result with a next step, not a failure
    assert not scale.called
    assert "cms.stuck" in out
    assert "Created with 0 workers" in out
    assert "still PENDING" in out
    assert "scale_dask_cluster('cms.stuck', 2)" in out


@respx.mock
async def test_create_reports_a_scheduler_that_failed(user_ctx):
    """The cluster exists even though its scheduler died, so the caller still
    gets its name rather than a bare failure."""
    respx.post(CLUSTERS).respond(201, json={"name": "cms.dead"})
    respx.get(f"{GATEWAY}/api/v1/clusters/cms.dead").respond(
        200, json={"name": "cms.dead", "status": "FAILED"}
    )
    scale = respx.post(f"{GATEWAY}/api/v1/clusters/cms.dead/scale").respond(204)

    out = await register_tools(dask).tools["create_dask_cluster"](
        FakeCtx(),
        env_source="global",
        worker_cores=1,
        worker_memory=4,
        n_workers=2,
    )

    assert not scale.called
    assert "cms.dead" in out
    assert "Created with 0 workers — waiting for the scheduler failed" in out
    assert "is FAILED" in out


@respx.mock
async def test_create_with_zero_workers_never_waits(user_ctx):
    """No scale to make, so nothing should poll the cluster record."""
    respx.post(CLUSTERS).respond(201, json={"name": "cms.empty"})
    status = respx.get(f"{GATEWAY}/api/v1/clusters/cms.empty")

    out = await register_tools(dask).tools["create_dask_cluster"](
        FakeCtx(),
        env_source="global",
        worker_cores=1,
        worker_memory=4,
        n_workers=0,
    )

    assert not status.called
    assert "starts with 0 workers" in out


@respx.mock
async def test_running_cluster_costs_one_status_check(user_ctx):
    """The normal path must not add polling latency."""
    status = scheduler_running("c1")
    respx.post(f"{GATEWAY}/api/v1/clusters/c1/scale").respond(204)

    await register_tools(dask).tools["scale_dask_cluster"]("c1", 1)

    assert status.call_count == 1


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
    respx.get(f"{GATEWAY}/api/v1/options").respond(
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
    assert "worker_cores" in out
    assert "pixi_project" in out


@respx.mock
async def test_create_cluster_explicit_args_pixi_then_scale(user_ctx):
    create = respx.post(CLUSTERS).respond(201, json={"name": "cms.abc"})
    scheduler_running("cms.abc")
    scale = respx.post(f"{GATEWAY}/api/v1/clusters/cms.abc/scale").respond(204)

    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](
        FakeCtx(),  # explicit args → no elicitation
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
async def test_create_cluster_explicit_conda_no_scale(user_ctx):
    create = respx.post(CLUSTERS).respond(201, json={"name": "cms.c1"})

    tools = register_tools(dask).tools
    out = await tools["create_dask_cluster"](
        FakeCtx(),
        conda_env="/depot/cms/alice/envs/ana",
        worker_cores=1,
        worker_memory=4,
        n_workers=0,
    )
    assert "cms.c1" in out
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
async def test_create_elicits_global_env(user_ctx):
    create = respx.post(CLUSTERS).respond(201, json={"name": "cms.g"})

    ctx = FakeCtx(
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
    assert len(ctx.calls) == 3


@respx.mock
async def test_create_elicits_pixi_path(user_ctx):
    create = respx.post(CLUSTERS).respond(201, json={"name": "cms.p"})

    ctx = FakeCtx(
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
    assert len(ctx.calls) == 4


@respx.mock
async def test_create_elicits_conda_path(user_ctx):
    create = respx.post(CLUSTERS).respond(201, json={"name": "cms.c"})

    ctx = FakeCtx(
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
    respx.post(CLUSTERS).respond(201, json={"name": "cms.n"})
    scheduler_running("cms.n")
    scale = respx.post(f"{GATEWAY}/api/v1/clusters/cms.n/scale").respond(204)

    ctx = FakeCtx(
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
    create = respx.post(CLUSTERS).respond(201, json={"name": "cms.x"})
    scheduler_running("cms.x")
    scale = respx.post(f"{GATEWAY}/api/v1/clusters/cms.x/scale").respond(204)

    ctx = FakeCtx(
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
    assert len(ctx.calls) == 5


def env_source(source):
    return accept(dask._EnvChoice(env_source=source))


CREATE_QUESTIONS = {
    # answers up to the one that fails, and the question it fails at
    "no-context": (None, None),
    "env": ([("decline", None)], dask._EnvChoice),
    "pixi-path": ([env_source("pixi"), ("cancel", None)], dask._PixiChoice),
    "conda-path": ([env_source("conda"), RuntimeError("boom")], dask._CondaChoice),
    "size": (
        [env_source("global"), NoBackChannelError("elicitation/create")],
        dask._SizeChoice,
    ),
    "custom-size": (
        [
            env_source("global"),
            accept(dask._SizeChoice(size="custom")),
            ("decline", None),
        ],
        dask._CustomSize,
    ),
    "count": (
        [env_source("global"), default_size(), ("cancel", None)],
        dask._CountChoice,
    ),
    "custom-count": (
        [env_source("global"), default_size(), count("custom"), RuntimeError("boom")],
        dask._CustomCount,
    ),
}


@pytest.mark.parametrize(
    ("answers", "question"), CREATE_QUESTIONS.values(), ids=CREATE_QUESTIONS.keys()
)
@respx.mock
async def test_create_falls_back_at_every_question(user_ctx, answers, question):
    ctx = answers and FakeCtx(*answers)
    tools = register_tools(dask).tools

    out = await needs_choices(tools["create_dask_cluster"](ctx))

    assert "create_dask_cluster needs" in out
    assert not respx.calls  # no cluster is created on a partial set of choices
    if ctx:
        assert len(ctx.calls) == len(answers)
        assert ctx.calls[-1][1] is question


# ── stop_dask_cluster ─────────────────────────────────────────────────────────


@respx.mock
async def test_stop_cluster(user_ctx):
    respx.delete(f"{GATEWAY}/api/v1/clusters/c1").respond(204)

    tools = register_tools(dask).tools
    out = await tools["stop_dask_cluster"]("c1")
    assert "stopped" in out


@respx.mock
async def test_stop_cluster_already_gone(user_ctx):
    respx.delete(f"{GATEWAY}/api/v1/clusters/c1").respond(404)

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
    respx.get(f"{GATEWAY}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )

    def responder(request):
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
    respx.get(f"{GATEWAY}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )

    def responder(request):
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
    respx.get(f"{GATEWAY}/api/v1/clusters/cms.abc").respond(
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


@respx.mock
async def test_worker_count_rejects_a_malformed_record(user_ctx):
    respx.get(f"{GATEWAY}/api/v1/clusters/c1").respond(200, json=["c1"])
    out = await failure(register_tools(dask).tools["get_dask_worker_count"]("c1"))
    assert "was not a cluster record" in out


@respx.mock
async def test_cluster_status_rejects_a_malformed_record(user_ctx):
    from errors import UpstreamError

    respx.get(f"{GATEWAY}/api/v1/clusters/c1").respond(200, json=["c1"])
    with pytest.raises(UpstreamError, match="was not a cluster record"):
        await dask._cluster_status("c1")


@respx.mock
async def test_create_reports_a_failed_scale(user_ctx):
    tools = register_tools(dask).tools
    kwargs = dict(
        pixi_project="/work/alice/p",
        worker_cores=1,
        worker_memory=4,
        n_workers=2,
    )

    respx.post(CLUSTERS).respond(201, json={"name": "cms.s"})
    scheduler_running("cms.s")
    respx.post(f"{GATEWAY}/api/v1/clusters/cms.s/scale").mock(
        side_effect=ConnectError("down")
    )
    out = await tools["create_dask_cluster"](FakeCtx(), **kwargs)
    assert "Created with 0 workers — the scale request failed" in out
    assert "unreachable" in out
    assert "scale_dask_cluster('cms.s', 2)" in out

    respx.post(CLUSTERS).respond(201, json={"name": "cms.s2"})
    scheduler_running("cms.s2")
    respx.post(f"{GATEWAY}/api/v1/clusters/cms.s2/scale").respond(500, text="no")
    out = await tools["create_dask_cluster"](FakeCtx(), **kwargs)
    assert "returned HTTP 500 while trying to scale to 2 worker(s)" in out


@respx.mock
async def test_worker_count_no_metrics(user_ctx):
    tools = register_tools(dask).tools
    respx.get(f"{GATEWAY}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )
    respx.get(f"{dask.PROMETHEUS_URL}/api/v1/query").respond(
        200, json={"data": {"result": []}}
    )
    out = await tools["get_dask_worker_count"]("cms.abc")
    assert "No worker metrics" in out


@respx.mock
async def test_usage_partial_metrics(user_ctx):
    tools = register_tools(dask).tools
    respx.get(f"{GATEWAY}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )

    def responder(request):
        q = request.url.params["query"]
        if "container_cpu_usage" in q:
            return httpx.Response(200, json=prom_vector([({"pod": "w1"}, 0.5)]))
        return httpx.Response(200, json={"data": {"result": []}})

    respx.get(f"{dask.CLUSTER_PROMETHEUS_URL}/api/v1/query").mock(side_effect=responder)
    out = await tools["get_dask_cluster_usage"]("cms.abc")
    assert "CPU (cores):" in out
    assert "Memory (GiB): no data" in out


@respx.mock
async def test_create_returns_build_options_error(user_ctx, monkeypatch):
    """If _build_cluster_options rejects after elicitation, surface that failure."""
    from errors import UserError

    tools = register_tools(dask).tools

    def reject(**kw):
        raise UserError("Error: synthetic options failure")

    monkeypatch.setattr(dask, "_build_cluster_options", reject)
    ctx = FakeCtx(
        accept(dask._EnvChoice(env_source="global")),
        accept(dask._SizeChoice(size="default")),
        count("0"),
    )
    out = await failure(tools["create_dask_cluster"](ctx))
    assert "synthetic options failure" in out


@respx.mock
async def test_usage_cpu_missing_memory_present(user_ctx):
    respx.get(f"{GATEWAY}/api/v1/clusters/cms.abc").respond(
        200, json={"name": "cms.abc", "status": "RUNNING"}
    )

    def responder(request):
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
async def test_create_relays_the_gateways_size_refusal(user_ctx):
    """The gateway owns the per-worker limits; its reason reaches the user."""
    respx.post(CLUSTERS).respond(
        422, text="422: worker_cores must be <= 64.000000, got 65.0"
    )
    out = await failure(
        register_tools(dask).tools["create_dask_cluster"](
            FakeCtx(), conda_env="/e", worker_cores=65, worker_memory=4, n_workers=0
        )
    )
    assert "Dask Gateway rejected the request to create the cluster" in out
    assert "worker_cores must be <= 64.000000, got 65.0" in out


CLAMPED = {
    "ok": False,
    "msg": "Scale request of 2000 workers would exceed resource limit of 1000 "
    "workers. Scaling to 1000 instead.",
}


def _running_cluster(name="cms.c1"):
    respx.get(f"{GATEWAY}/api/v1/clusters/{name}").respond(
        200, json={"name": name, "status": "RUNNING"}
    )
    return respx.post(f"{GATEWAY}/api/v1/clusters/{name}/scale")


@respx.mock
async def test_scale_relays_the_gateways_clamp(user_ctx):
    _running_cluster().respond(200, json=CLAMPED)
    out = await register_tools(dask).tools["scale_dask_cluster"]("cms.c1", 2000)
    assert CLAMPED["msg"] in out
    assert "scaling to 2000" not in out


@respx.mock
async def test_create_relays_the_gateways_clamp(user_ctx):
    respx.post(CLUSTERS).respond(201, json={"name": "cms.c1"})
    _running_cluster().respond(200, json=CLAMPED)
    out = await register_tools(dask).tools["create_dask_cluster"](
        FakeCtx(), conda_env="/e", worker_cores=1, worker_memory=4, n_workers=2000
    )
    assert CLAMPED["msg"] in out
    assert "Scaling to 2000" not in out


@respx.mock
async def test_a_refused_scale_without_a_reason_is_still_reported(user_ctx):
    _running_cluster().respond(200, json={"ok": False, "msg": None})
    out = await register_tools(dask).tools["scale_dask_cluster"]("cms.c1", 5)
    assert "did not take that worker count" in out


@respx.mock
@pytest.mark.parametrize("body", [{"ok": True, "msg": None}, None])
async def test_a_taken_count_reads_as_scaling(user_ctx, body):
    _running_cluster().respond(200, json=body)
    out = await register_tools(dask).tools["scale_dask_cluster"]("cms.c1", 5)
    assert "scaling to 5 worker(s)" in out


@respx.mock
async def test_scale_allows_the_gateway_time_to_forward(user_ctx):
    route = _running_cluster().respond(200, json={"ok": True, "msg": None})
    await register_tools(dask).tools["scale_dask_cluster"]("cms.c1", 5)
    assert route.calls[0].request.extensions["timeout"]["read"] == 30.0


@respx.mock
async def test_worker_count_escapes_username_in_promql():
    """A username with quotes cannot break out of the PromQL label matcher."""
    from shared import quote_label

    hostile = 'ali"} or {job!="'
    with as_user(username=hostile):
        respx.get(f"{GATEWAY}/api/v1/clusters/cms.abc").respond(
            200, json={"name": "cms.abc", "status": "RUNNING"}
        )
        seen = []

        def responder(request):
            seen.append(request.url.params["query"])
            return httpx.Response(200, json=prom_scalar(1))

        respx.get(f"{dask.PROMETHEUS_URL}/api/v1/query").mock(side_effect=responder)

        tools = register_tools(dask).tools
        await tools["get_dask_worker_count"]("cms.abc")

    assert seen
    for q in seen:
        assert f'user="{hostile}"' not in q  # raw interpolation would inject
        assert quote_label(hostile) in q


@respx.mock
async def test_usage_regex_escapes_cluster_id(user_ctx):
    """The pod regex uses re.escape on the cluster id (defense in depth)."""
    respx.get(f"{GATEWAY}/api/v1/clusters/abc-1").respond(
        200, json={"name": "abc-1", "status": "RUNNING"}
    )
    seen = []

    def responder(request):
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
async def test_worker_count_prometheus_down_is_not_no_metrics(user_ctx):
    respx.get(f"{GATEWAY}/api/v1/clusters/cms.abc").respond(
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
    respx.get(f"{GATEWAY}/api/v1/clusters/cms.abc").respond(
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

CREATE = dict(pixi_project="/p", worker_cores=1, worker_memory=1, n_workers=0)
INFO, SCALE, OPTIONS = (
    f"{GATEWAY}/api/v1/clusters/c1",
    f"{GATEWAY}/api/v1/clusters/c1/scale",
    f"{GATEWAY}/api/v1/options",
)
DOWN = ConnectError("down")


def _resp(status, **kwargs):
    return httpx.Response(status, **kwargs)


# fmt: off
GATEWAY_FAILURES = [
    # tool, arguments, method, url, gateway answer, Failure class, message fragments
    ("list_dask_clusters", {}, "GET", CLUSTERS, DOWN, "UpstreamError", ["Error: Dask Gateway unreachable — connection failed (down)"]),
    ("list_dask_clusters", {}, "GET", CLUSTERS, _resp(401), "AuthError", ["not authorised on Dask Gateway to list clusters (HTTP 401)"]),
    ("list_dask_clusters", {}, "GET", CLUSTERS, _resp(503, json={"message": "scheduler restarting"}), "UpstreamError", ["HTTP 503", "scheduler restarting"]),
    ("list_dask_clusters", {}, "GET", CLUSTERS, _resp(200, text="<html>proxy</html>"), "UpstreamError", ["not a cluster list"]),
    ("get_dask_cluster_info", {"cluster_name": "c1"}, "GET", INFO, DOWN, "UpstreamError", ["Error: Dask Gateway unreachable — connection failed (down)"]),
    ("get_dask_cluster_info", {"cluster_name": "c1"}, "GET", INFO, _resp(500, text="err"), "UpstreamError", ["HTTP 500"]),
    ("get_dask_cluster_info", {"cluster_name": "c1"}, "GET", INFO, _resp(502, text="<h1>Bad Gateway</h1>"), "UpstreamError", ["returned HTTP 502 while trying to inspect cluster 'c1' (it is down or restarting behind its proxy) — Bad Gateway"]),
    ("get_dask_cluster_info", {"cluster_name": "c1"}, "GET", INFO, _resp(404), "UserError", ["Cluster 'c1' not found.", "list_dask_clusters"]),
    ("get_dask_cluster_info", {"cluster_name": "c1"}, "GET", INFO, _resp(403), "AuthError", ["Error: not authorised on Dask Gateway to inspect cluster 'c1' (HTTP 403).", "AF support"]),
    ("get_dask_cluster_info", {"cluster_name": "c1"}, "GET", INFO, _resp(200, text="nope"), "UpstreamError", ["not a cluster record"]),
    ("get_dask_worker_count", {"cluster_name": "c1"}, "GET", INFO, DOWN, "UpstreamError", ["unreachable"]),
    ("get_dask_worker_count", {"cluster_name": "c1"}, "GET", INFO, _resp(403), "AuthError", ["not authorised"]),
    ("get_dask_worker_count", {"cluster_name": "c1"}, "GET", INFO, _resp(404), "UserError", ["Cluster 'c1' not found."]),
    ("get_dask_worker_count", {"cluster_name": "c1"}, "GET", INFO, _resp(500, text="boom"), "UpstreamError", ["HTTP 500"]),
    ("get_dask_cluster_usage", {"cluster_name": "c1"}, "GET", INFO, _resp(403), "AuthError", ["not authorised"]),
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": 3}, "POST", SCALE, DOWN, "UpstreamError", ["unreachable"]),
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": 3}, "POST", SCALE, _resp(500, text="no"), "UpstreamError", ["HTTP 500"]),
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": 3}, "POST", SCALE, _resp(404), "UserError", ["Cluster 'c1' not found.", "list_dask_clusters"]),
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": 3}, "POST", SCALE, _resp(409, json={"message": "cluster is stopping"}), "UserError", ["Error: Dask Gateway rejected the request to scale to 3 worker(s) — cluster is stopping."]),
    ("stop_dask_cluster", {"cluster_name": "c1"}, "DELETE", INFO, DOWN, "UpstreamError", ["unreachable"]),
    ("stop_dask_cluster", {"cluster_name": "c1"}, "DELETE", INFO, _resp(500, text="boom"), "UpstreamError", ["HTTP 500", "boom"]),
    ("list_dask_cluster_options", {}, "GET", OPTIONS, DOWN, "UpstreamError", ["unreachable"]),
    ("list_dask_cluster_options", {}, "GET", OPTIONS, _resp(401), "AuthError", ["not authorised on Dask Gateway to list cluster options (HTTP 401)"]),
    ("list_dask_cluster_options", {}, "GET", OPTIONS, _resp(500, text="nope"), "UpstreamError", ["HTTP 500"]),
    ("list_dask_cluster_options", {}, "GET", OPTIONS, _resp(200, text="nope"), "UpstreamError", ["not a cluster-options document"]),
    ("create_dask_cluster", CREATE, "POST", CLUSTERS, DOWN, "UpstreamError", ["Dask Gateway unreachable"]),
    ("create_dask_cluster", CREATE, "POST", CLUSTERS, _resp(500, text="boom"), "UpstreamError", ["HTTP 500"]),
    ("create_dask_cluster", CREATE, "POST", CLUSTERS, _resp(422, json={"message": "User already has 1 active clusters"}), "UserError", ["rejected", "active clusters"]),
    ("create_dask_cluster", CREATE, "POST", CLUSTERS, _resp(422, text="plain reject"), "UserError", ["rejected", "plain reject"]),
    ("create_dask_cluster", CREATE, "POST", CLUSTERS, _resp(201, text="<html>proxy</html>"), "UpstreamError", ["not a cluster record with a name", "list_dask_clusters before creating another"]),
    ("create_dask_cluster", CREATE, "POST", CLUSTERS, _resp(201, json={}), "UpstreamError", ["not a cluster record with a name"]),
]

REJECTED_ARGUMENTS = [
    # tool, arguments, message fragment — refused before anything leaves the process
    ("scale_dask_cluster", {"cluster_name": "c1", "n_workers": -1}, "must be ≥ 0"),
    ("create_dask_cluster", {"n_workers": -1}, "n_workers"),
    ("create_dask_cluster", {"worker_cores": 0, "pixi_project": "/p"}, "worker_cores"),
    ("create_dask_cluster", {"worker_memory": -1, "pixi_project": "/p"}, "worker_memory"),
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

    # scale_dask_cluster waits for the scheduler before scaling; these cases
    # are about how the scale POST itself is translated, so let that check pass.
    if tool == "scale_dask_cluster":
        scheduler_running(arguments["cluster_name"])

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
