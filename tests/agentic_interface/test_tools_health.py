"""Tests for tools/health.py — the "is the AF healthy" answer.

The tool reads whatever Prometheus is firing, so these stub Prometheus and
assert on the resulting prose. Three properties matter more than wording:
a warning must not make the facility look broken, an unknown must not look
healthy, and no other user's alert may ever appear."""

import time

import pytest
import respx
from agentic_helpers import register_tools
from httpx import Response
from tools import health

PROM_URL = f"{health.PROMETHEUS_URL}/api/v1/query"
NOW = time.time()


def series(name, severity, component, **labels):
    return {
        "metric": {
            "alertname": name,
            "severity": severity,
            "component": component,
            **labels,
        },
        "value": [NOW, "1"],
    }


def prom(firing=(), since=(), recent=(), home_util=0.41, home_kb=25 * 1024 * 1024):
    """Route each query this tool makes to a canned result."""

    def responder(request):
        query = request.url.params.get("query", "")
        if 'alertstate="firing"' in query:
            result = list(firing)
        elif query == "ALERTS_FOR_STATE":
            result = list(since)
        elif "changes" in query:
            result = list(recent)
        elif "af_home_dir_util" in query:
            result = [{"value": [NOW, str(home_util)]}] if home_util is not None else []
        elif "af_home_dir_size_kb" in query:
            result = [{"value": [NOW, str(home_kb)]}]
        else:
            result = []
        return Response(200, json={"data": {"result": result}})

    return responder


async def run(**kwargs):
    tools = register_tools(health)
    with respx.mock:
        respx.get(PROM_URL).mock(side_effect=prom(**kwargs))
        return await tools.tools["get_facility_health"]()


@pytest.mark.asyncio
async def test_healthy_when_nothing_is_firing(user_ctx):
    out = await run()
    assert "**Healthy**" in out
    assert "Nothing is failing" in out


@pytest.mark.asyncio
async def test_warning_alone_does_not_degrade_the_facility(user_ctx):
    """A routine warning must not turn the headline red, or it stops meaning
    anything."""
    out = await run(firing=[series("AFMountHealthUnknown", "warning", "data")])
    assert "**Degraded**" not in out


@pytest.mark.asyncio
async def test_unknown_storage_is_not_reported_as_healthy(user_ctx):
    out = await run(firing=[series("AFMountHealthUnknown", "warning", "data")])
    assert "**Partly unknown**" in out
    assert "not the same as broken" in out


@pytest.mark.asyncio
async def test_error_degrades_the_facility(user_ctx):
    out = await run(
        firing=[series("AFMountInvalid", "error", "data", mount_name="eos")]
    )
    assert "**Degraded**" in out


@pytest.mark.asyncio
async def test_widespread_failure_reads_as_a_system_problem(user_ctx):
    """Storage rarely breaks on one machine; the wording has to distinguish."""
    many = [
        series(
            "AFMountInvalid", "error", "data", mount_name="eos", exported_node=f"n{i}"
        )
        for i in range(14)
    ]
    out = await run(firing=many)
    assert "14 nodes" in out
    assert "storage-system or network" in out

    one = [
        series("AFMountInvalid", "error", "data", mount_name="eos", exported_node="n1")
    ]
    out = await run(firing=one)
    assert "single machine" in out


@pytest.mark.asyncio
async def test_never_reports_another_users_alert(user_ctx):
    """af-pod-monitor alerts carry a username; only the caller's own may show."""
    out = await run(
        firing=[
            series("AFHomeDirUtilHigh", "warning", "storage", username="someone-else")
        ]
    )
    assert "someone-else" not in out
    assert "AFHomeDirUtilHigh" not in out
    assert "**Healthy**" in out


@pytest.mark.asyncio
async def test_a_hidden_alert_is_not_counted_as_resolved(user_ctx):
    """It is still firing — hiding it from this user must not turn it into
    "came and went"."""
    hidden = series("AFHomeDirUtilHigh", "warning", "storage", username="someone-else")
    out = await run(
        firing=[hidden], recent=[{"metric": {"alertname": "AFHomeDirUtilHigh"}}]
    )
    assert "came and went" not in out


@pytest.mark.asyncio
async def test_times_are_eastern_and_labelled(user_ctx):
    since = [
        {"metric": {"alertname": "AFMountInvalid"}, "value": [NOW, str(NOW - 7200)]}
    ]
    out = await run(
        firing=[series("AFMountInvalid", "error", "data", mount_name="eos")],
        since=since,
    )
    assert " ET, " in out
    assert "ago)" in out


@pytest.mark.asyncio
async def test_reports_the_callers_own_quota(user_ctx):
    out = await run(home_util=0.93)
    assert "93%" in out
    assert "Approaching the limit" in out


@pytest.mark.asyncio
async def test_monitoring_outage_is_not_a_health_claim(user_ctx):
    """If Prometheus cannot be reached, say so rather than implying healthy."""
    out = await run(home_util=None)
    assert "cannot tell you" in out
    assert "**Healthy**" not in out


@pytest.mark.asyncio
async def test_output_avoids_internal_mechanics(user_ctx):
    """Users get facility facts, not the monitoring implementation."""
    out = await run(
        firing=[series("AFMountInvalid", "error", "data", mount_name="eos")]
    )
    for jargon in ("probe", "Prometheus", "af_node_mount", "kubectl", "Job"):
        assert jargon not in out


@pytest.mark.asyncio
async def test_dev_hardware_is_invisible_to_users(user_ctx):
    """Dev nodes are monitored for operators but run no user sessions; a user
    asking about the facility must not see them, and they must not degrade it."""
    out = await run(
        firing=[
            series("AFMountInvalidDev", "warning", "dev", exported_node="a337"),
            series(
                "AFMountInvalid", "error", "data", node_pool="dev", exported_node="a337"
            ),
        ]
    )
    assert "**Healthy**" in out
    assert "a337" not in out
    assert "dev" not in out.lower().replace("device", "")
