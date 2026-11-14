"""Tests for tools/promql.py: what runs is the scoped query, never the caller's
own text, and the answer reads well."""

import pytest
import respx
from agentic_helpers import failure, register_tools
from httpx import ConnectError, Response
from promql_scope import Owned
from tools import promql

AF = promql.PROMETHEUS_URL
CLUSTER = promql.CLUSTER_PROMETHEUS_URL


@pytest.fixture
def scoped(monkeypatch):
    """resolve() and scope() without Prometheus or the Go helper: the caller is
    alice, and a scoped query reads SCOPED(<query>)."""
    resolved = []

    async def resolve(client, username, start, end):
        resolved.append((username, start, end))
        return Owned(username)

    async def scope(query, owned):
        return f"SCOPED({query})"

    monkeypatch.setattr(promql, "resolve", resolve)
    monkeypatch.setattr(promql, "scope", scope)
    return resolved


@pytest.fixture
def tools(scoped, user_ctx):
    return register_tools(promql).tools


def vector(*rows):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": m, "value": [1, str(v)]} for m, v in rows],
        },
    }


@respx.mock
async def test_an_instant_query_runs_scoped(tools, scoped):
    route = respx.post(f"{AF}/api/v1/query").mock(
        return_value=Response(200, json=vector(({"__name__": "up", "job": "loki"}, 1)))
    )
    out = await tools["query_prometheus"]("up")
    form = dict(
        x.split("=", 1) for x in route.calls[0].request.content.decode().split("&")
    )
    assert form["query"] == "SCOPED%28up%29"
    assert 'up{job="loki"}: 1' in out
    assert "list_af_profiles" in out
    assert scoped[0][0] == "alice"


@respx.mock
async def test_a_range_query_on_the_cluster(tools, scoped):
    route = respx.post(f"{CLUSTER}/api/v1/query_range").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {"metric": {"pod": "hub-1"}, "values": [[0, "1"], [60, "2.5"]]}
                    ],
                }
            },
        )
    )
    out = await tools["query_prometheus"](
        "rate(x[5m])", source="cluster", start="6h", step="5m"
    )
    sent = route.calls[0].request.content.decode()
    assert "step=300" in sent
    assert '{pod="hub-1"} from 1970-01-01T00:00:00Z: 1, 2.5' in out
    username, start, end = scoped[0]
    assert end - start == pytest.approx(6 * 3600 + promql.OWNED_LOOKBACK, abs=2)


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"source": "nrp"}, "neither 'af' nor 'cluster'"),
        ({"start": "40d"}, "at most 30d"),
        ({"start": "yesterday"}, "neither 'now'"),
        ({"start": "1h", "end": "2h"}, "start must come before end"),
        ({"start": "30d", "step": "15s"}, "step of at least"),
        ({"start": "1h", "step": "0s"}, "at least 1s"),
        ({"start": "1h", "step": "fast"}, "not a duration"),
    ],
)
async def test_bad_arguments(tools, kwargs, expected):
    assert expected in await failure(tools["query_prometheus"]("up", **kwargs))


@respx.mock
async def test_prometheus_refusing_the_query_is_the_callers_error(tools):
    respx.post(f"{AF}/api/v1/query").mock(
        return_value=Response(
            400,
            json={"status": "error", "errorType": "bad_data", "error": "parse error"},
        )
    )
    message = await failure(tools["query_prometheus"]("up"))
    assert "refused the query — parse error" in message


@respx.mock
async def test_unreachable_prometheus(tools):
    respx.post(f"{AF}/api/v1/query").mock(side_effect=ConnectError("down"))
    assert "unreachable" in await failure(tools["query_prometheus"]("up"))


@respx.mock
async def test_many_series_are_cut(tools):
    rows = [({"pod": f"p{i:03}"}, i) for i in range(promql.MAX_SERIES + 5)]
    respx.post(f"{AF}/api/v1/query").mock(
        return_value=Response(200, json=vector(*rows))
    )
    out = await tools["query_prometheus"]("x")
    assert f"{promql.MAX_SERIES + 5} series, first {promql.MAX_SERIES}" in out
    assert "p054" not in out


@respx.mock
async def test_list_metrics_names_only_what_the_caller_can_read(tools):
    names = respx.post(f"{CLUSTER}/api/v1/query").mock(
        return_value=Response(
            200,
            json=vector(
                ({"__name__": "container_memory_working_set_bytes"}, 1),
                ({"__name__": "up"}, 1),
            ),
        )
    )
    respx.get(f"{CLUSTER}/api/v1/metadata").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "container_memory_working_set_bytes": [
                        {"type": "gauge", "help": "Current working set"}
                    ],
                    "secret_metric_of_another_namespace": [
                        {"type": "gauge", "help": "x"}
                    ],
                }
            },
        )
    )
    out = await tools["list_prometheus_metrics"]("memory", source="cluster")
    assert "SCOPED(group by (__name__)" in names.calls[
        0
    ].request.content.decode().replace("+", " ").replace("%28", "(").replace("%29", ")")
    assert "container_memory_working_set_bytes (gauge): Current working set" in out
    assert "secret_metric" not in out
    assert "- up" not in out


@respx.mock
async def test_a_query_prometheus_gives_up_on_says_how_to_narrow_it(tools):
    respx.post(f"{AF}/api/v1/query").mock(
        return_value=Response(
            503, json={"status": "error", "errorType": "timeout", "error": "timed out"}
        )
    )
    assert "Narrow the query" in await failure(tools["query_prometheus"]("up"))


@respx.mock
async def test_a_scalar_and_an_iso_window(tools, scoped):
    route = respx.post(f"{AF}/api/v1/query_range").mock(
        return_value=Response(
            200, json={"data": {"resultType": "matrix", "result": []}}
        )
    )
    out = await tools["query_prometheus"](
        "x", start="2026-11-01T00:00:00", end="2026-11-02T00:00:00Z"
    )
    assert "start=1793491200.000" in route.calls[0].request.content.decode()
    assert "No series match." in out
    respx.post(f"{AF}/api/v1/query").mock(
        return_value=Response(
            200, json={"data": {"resultType": "scalar", "result": [1, "2.5"]}}
        )
    )
    assert "scalar: 2.5" in await tools["query_prometheus"]("scalar(x)", end="now")


@respx.mock
async def test_list_metrics_with_no_match(tools):
    respx.post(f"{AF}/api/v1/query").mock(
        return_value=Response(200, json=vector(({"__name__": "up"}, 1)))
    )
    respx.get(f"{AF}/api/v1/metadata").mock(return_value=Response(200, json={}))
    out = await tools["list_prometheus_metrics"]("gpu")
    assert out == "No metric on af matches 'gpu'."
