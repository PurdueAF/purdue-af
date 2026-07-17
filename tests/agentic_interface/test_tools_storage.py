"""Tests for tools/storage.py — Prometheus scalar queries and quota report."""

import re

import respx
from agentic_helpers import register_tools
from httpx import AsyncClient, ConnectError
from tools import storage

PROM_URL = f"{storage.PROMETHEUS_URL}/api/v1/query"


def prom_result(value):
    return {"data": {"result": [{"value": [1700000000, str(value)]}]}}


# ── _prom_scalar ──────────────────────────────────────────────────────────────


@respx.mock
async def test_prom_scalar_returns_float():
    respx.get(PROM_URL).respond(200, json=prom_result(42.5))
    async with AsyncClient() as client:
        assert await storage._prom_scalar(client, "q") == 42.5


@respx.mock
async def test_prom_scalar_empty_result_is_none():
    respx.get(PROM_URL).respond(200, json={"data": {"result": []}})
    async with AsyncClient() as client:
        assert await storage._prom_scalar(client, "q") is None


@respx.mock
async def test_prom_scalar_http_error_is_none():
    respx.get(PROM_URL).respond(503)
    async with AsyncClient() as client:
        assert await storage._prom_scalar(client, "q") is None


@respx.mock
async def test_prom_scalar_unreachable_is_none():
    respx.get(PROM_URL).mock(side_effect=ConnectError("down"))
    async with AsyncClient() as client:
        assert await storage._prom_scalar(client, "q") is None


@respx.mock
async def test_prom_scalar_malformed_value_is_none():
    respx.get(PROM_URL).respond(
        200, json={"data": {"result": [{"value": [1700000000, "NaN-ish?"]}]}}
    )
    async with AsyncClient() as client:
        assert await storage._prom_scalar(client, "q") is None


# ── _bar ──────────────────────────────────────────────────────────────────────


def test_bar_renders_and_clamps():
    assert storage._bar(0.0) == "░" * 20
    assert storage._bar(1.0) == "█" * 20
    assert storage._bar(0.5) == "█" * 10 + "░" * 10
    assert storage._bar(5.0) == "█" * 20  # clamps above 1
    assert storage._bar(-1.0) == "░" * 20  # clamps below 0


# ── query_storage_usage ───────────────────────────────────────────────────────


@respx.mock
async def test_storage_reports_usage(user_ctx):
    gb = 1024 * 1024  # KB per GB

    def responder(request):
        import httpx

        q = request.url.params["query"]
        value = {
            "af_home_dir_used_kb": 5 * gb,
            "af_home_dir_size_kb": 25 * gb,
            "af_home_dir_util": 0.2,
            "af_home_dir_last_accessed": 1700000000,
            "af_work_dir_used_kb": 50 * gb,
            "af_work_dir_size_kb": 100 * gb,
            "af_work_dir_util": 0.5,
            "af_work_dir_last_accessed": 1700000000,
        }[q.split("{")[0]]
        return httpx.Response(200, json=prom_result(value))

    route = respx.get(PROM_URL).mock(side_effect=responder)

    tools = register_tools(storage).tools
    out = await tools["query_storage_usage"]()

    assert "5.00 GB / 25.00 GB" in out
    assert "20.0%" in out
    assert "50.00 GB / 100.00 GB" in out
    assert "50.0%" in out
    assert "last accessed 2023-11-14" in out
    # every query is scoped to the authenticated username
    for call in route.calls:
        assert 'username="alice"' in call.request.url.params["query"]

@respx.mock
async def test_storage_no_metrics_message(user_ctx):
    respx.get(PROM_URL).respond(200, json={"data": {"result": []}})

    tools = register_tools(storage).tools
    out = await tools["query_storage_usage"]()
    assert "No storage metrics" in out


@respx.mock
async def test_storage_partial_data_marks_missing_dir(user_ctx):
    def responder(request):
        import httpx

        q = request.url.params["query"]
        if q.startswith("af_home"):
            return httpx.Response(200, json=prom_result(1024 * 1024))
        return httpx.Response(200, json={"data": {"result": []}})

    respx.get(PROM_URL).mock(side_effect=responder)

    tools = register_tools(storage).tools
    out = await tools["query_storage_usage"]()
    assert "/work/: no data" in out
    assert "/home/" in out


@respx.mock
async def test_storage_util_falls_back_to_ratio(user_ctx):
    def responder(request):
        import httpx

        q = request.url.params["query"]
        if "util" in q or "last_accessed" in q:
            return httpx.Response(200, json={"data": {"result": []}})
        if "used" in q:
            return httpx.Response(200, json=prom_result(10 * 1024 * 1024))
        return httpx.Response(200, json=prom_result(40 * 1024 * 1024))

    respx.get(PROM_URL).mock(side_effect=responder)

    tools = register_tools(storage).tools
    out = await tools["query_storage_usage"]()
    # 10/40 = 25% computed from the used/size ratio
    assert re.search(r"25\.0%", out)
