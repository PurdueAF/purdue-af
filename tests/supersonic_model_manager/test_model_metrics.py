"""Per-model Prometheus queries."""

import httpx
import pytest
import respx
from model_manager import metrics
from model_manager.config import settings

PROM = "http://prometheus:9090"


@pytest.fixture(autouse=True)
def prometheus(monkeypatch):
    monkeypatch.setattr(settings, "prometheus_url", PROM)
    monkeypatch.setattr(settings, "prometheus_selector", 'release="supersonic"')
    monkeypatch.setattr(settings, "prometheus_window", "5m")
    monkeypatch.setattr(settings, "prometheus_timeout_s", 1)


def vector(*pairs):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"model": name}, "value": [1700000000, value]}
                for name, value in pairs
            ],
        },
    }


def test_queries_carry_the_configured_selector_and_window():
    queries = metrics._queries()

    assert all('release="supersonic"' in q for q in queries.values())
    assert "[5m]" in queries["throughput"]
    assert "sum by (model)" in queries["throughput"]


def test_selector_is_omitted_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "prometheus_selector", "")

    assert "{" not in metrics._queries()["inferenceCount"].split("(")[-1]


async def test_returns_nothing_when_prometheus_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "prometheus_url", "")

    result = await metrics.collect_metrics()

    assert result == {"models": {}, "error": None, "configured": False}


@respx.mock
async def test_collects_values_per_model():
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=vector(("deepmet", "12.5")))
    )

    result = await metrics.collect_metrics()

    assert result["configured"] is True
    assert result["error"] is None
    assert result["models"]["deepmet"]["throughput"] == 12.5


@respx.mock
async def test_drops_nan_from_idle_servers():
    """An idle model divides by a zero rate; Prometheus returns NaN."""
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=vector(("idle", "NaN"), ("busy", "3")))
    )

    result = await metrics.collect_metrics()

    assert "idle" not in result["models"]
    assert result["models"]["busy"]["throughput"] == 3.0


@respx.mock
async def test_drops_infinities():
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=vector(("m", "+Inf")))
    )

    result = await metrics.collect_metrics()

    assert result["models"] == {}


@respx.mock
async def test_query_failure_is_reported_without_breaking_the_dashboard():
    respx.get(f"{PROM}/api/v1/query").mock(side_effect=httpx.ConnectError("refused"))

    result = await metrics.collect_metrics()

    assert result["models"] == {}
    assert "ConnectError" in result["error"]


@respx.mock
async def test_series_without_a_model_label_are_ignored():
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {"job": "triton"}, "value": [0, "1"]}],
                },
            },
        )
    )

    result = await metrics.collect_metrics()

    assert result["models"] == {}
