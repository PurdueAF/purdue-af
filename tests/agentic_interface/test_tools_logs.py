"""Tests for tools/logs.py — time parsing, dedup, and Loki queries."""

import time
from urllib.parse import parse_qs, urlparse

import respx
from agentic_helpers import register_tools
from httpx import ConnectError
from tools import logs

LOKI_RANGE_URL = f"{logs.LOKI_URL}/loki/api/v1/query_range"
NOW = 1_700_000_000.0


# ── _to_ns ────────────────────────────────────────────────────────────────────


def test_to_ns_now():
    assert logs._to_ns("now", NOW) == str(int(NOW * 1e9))


def test_to_ns_durations():
    assert logs._to_ns("2d", NOW) == str(int((NOW - 2 * 86400) * 1e9))
    assert logs._to_ns("1h", NOW) == str(int((NOW - 3600) * 1e9))
    assert logs._to_ns("30m", NOW) == str(int((NOW - 1800) * 1e9))
    assert logs._to_ns("90s", NOW) == str(int((NOW - 90) * 1e9))


def test_to_ns_iso_with_zulu():
    assert logs._to_ns("2023-11-14T22:13:20Z", NOW) == str(int(NOW * 1e9))


def test_to_ns_iso_naive_assumed_utc():
    assert logs._to_ns("2023-11-14T22:13:20", NOW) == str(int(NOW * 1e9))


def test_to_ns_iso_with_offset():
    # 22:13:20 UTC == 17:13:20 -05:00
    assert logs._to_ns("2023-11-14T17:13:20-05:00", NOW) == str(int(NOW * 1e9))


def test_to_ns_garbage_passes_through():
    assert logs._to_ns("not-a-time", NOW) == "not-a-time"


# ── _dedup ────────────────────────────────────────────────────────────────────


def line(ts, msg, pod="pod-a"):
    return f"[{ts}] {pod}/notebook: {msg}"


def test_dedup_empty():
    assert logs._dedup([]) == []


def test_dedup_no_duplicates():
    lines = [line("t1", "a"), line("t2", "b")]
    assert logs._dedup(lines) == lines


def test_dedup_collapses_consecutive_identical():
    lines = [line("t1", "boom"), line("t2", "boom"), line("t3", "boom")]
    assert logs._dedup(lines) == [
        line("t1", "boom"),
        "  ↑ × 3 identical lines omitted",
    ]


def test_dedup_trailing_run_is_flushed():
    lines = [line("t1", "a"), line("t2", "b"), line("t3", "b")]
    out = logs._dedup(lines)
    assert out[-1] == "  ↑ × 2 identical lines omitted"


def test_dedup_does_not_collapse_across_pods():
    lines = [line("t1", "boom", "pod-a"), line("t2", "boom", "pod-b")]
    assert logs._dedup(lines) == lines


# ── Loki query tools ──────────────────────────────────────────────────────────


def loki_response(values, pod="purdue-af-alice", container="notebook"):
    return {
        "data": {
            "result": [
                {
                    "stream": {"pod": pod, "container": container},
                    "values": values,
                }
            ]
        }
    }


def query_of(route):
    request = route.calls.last.request
    return parse_qs(urlparse(str(request.url)).query)


@respx.mock
async def test_notebook_logs_selector_and_output(user_ctx):
    ts_ns = str(int(NOW * 1e9))
    route = respx.get(LOKI_RANGE_URL).respond(
        200, json=loki_response([[ts_ns, "hello world"]])
    )

    tools = register_tools(logs).tools
    out = await tools["query_notebook_logs"]()

    q = query_of(route)["query"][0]
    assert 'namespace="cms"' in q
    assert 'username="alice"' in q
    assert 'container="notebook"' in q
    assert "pod=" not in q
    assert "hello world" in out
    assert "# 1 line(s)" in out


@respx.mock
async def test_notebook_logs_applies_filter(user_ctx):
    route = respx.get(LOKI_RANGE_URL).respond(200, json=loki_response([]))

    tools = register_tools(logs).tools
    await tools["query_notebook_logs"](filter='|= "ERROR"')

    assert query_of(route)["query"][0].endswith('|= "ERROR"')


@respx.mock
async def test_dask_logs_excludes_notebook_container(user_ctx):
    route = respx.get(LOKI_RANGE_URL).respond(200, json=loki_response([]))

    tools = register_tools(logs).tools
    await tools["query_dask_logs"]()

    q = query_of(route)["query"][0]
    assert 'username="alice"' in q
    assert 'container!="notebook"' in q
    assert "pod!" not in q


@respx.mock
async def test_no_logs_message(user_ctx):
    respx.get(LOKI_RANGE_URL).respond(200, json={"data": {"result": []}})

    tools = register_tools(logs).tools
    out = await tools["query_notebook_logs"]()
    assert out == "No logs found for the specified time range."


@respx.mock
async def test_loki_http_error_is_reported(user_ctx):
    respx.get(LOKI_RANGE_URL).respond(500, text="overloaded")

    tools = register_tools(logs).tools
    out = await tools["query_notebook_logs"]()
    assert "HTTP 500" in out
    assert "overloaded" in out


@respx.mock
async def test_loki_unreachable_is_reported(user_ctx):
    respx.get(LOKI_RANGE_URL).mock(side_effect=ConnectError("down"))

    tools = register_tools(logs).tools
    out = await tools["query_notebook_logs"]()
    assert "Loki connection failed" in out


@respx.mock
async def test_limit_is_capped_at_5000(user_ctx):
    route = respx.get(LOKI_RANGE_URL).respond(200, json=loki_response([]))

    tools = register_tools(logs).tools
    await tools["query_notebook_logs"](limit=999_999)

    assert query_of(route)["limit"][0] == "5000"


@respx.mock
async def test_limit_reached_hint(user_ctx):
    ts_ns = str(int(time.time() * 1e9))
    values = [[ts_ns, f"msg {i}"] for i in range(3)]
    respx.get(LOKI_RANGE_URL).respond(200, json=loki_response(values))

    tools = register_tools(logs).tools
    out = await tools["query_notebook_logs"](limit=3, dedup=False)
    assert "limit=3 reached" in out


@respx.mock
async def test_dedup_flag_controls_collapsing(user_ctx):
    ts_ns = str(int(time.time() * 1e9))
    values = [[ts_ns, "same"], [ts_ns, "same"]]
    respx.get(LOKI_RANGE_URL).respond(200, json=loki_response(values))

    tools = register_tools(logs).tools
    collapsed = await tools["query_notebook_logs"](dedup=True)
    raw = await tools["query_notebook_logs"](dedup=False)

    assert "identical lines omitted" in collapsed
    assert "identical lines omitted" not in raw


@respx.mock
async def test_loki_request_uses_start_end_and_direction(user_ctx):
    route = respx.get(LOKI_RANGE_URL).respond(200, json=loki_response([]))

    tools = register_tools(logs).tools
    await tools["query_notebook_logs"](start="2h", end="1h", direction="backward")

    q = query_of(route)
    assert int(q["start"][0]) < int(q["end"][0])
    assert q["direction"][0] == "backward"
