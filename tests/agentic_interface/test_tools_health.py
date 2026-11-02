"""Tests for tools/health.py — the "is the AF healthy" answer.

The tool reads whatever Prometheus is firing (its alerts endpoint, which
carries each rule's rendered ``annotations.summary``), so these stub that
endpoint and assert on the resulting prose. Properties that matter more than
wording: a warning must not make the facility look **Degraded**, storage
slowness must show as **Impaired** with a per-component line, mount failures
are warnings (Impaired) not errors, an unknown must not look healthy, the
rule's own summary is what the user reads, and no other user's alert may
ever appear."""

import json
import time
from datetime import datetime, timezone

import pytest
import respx
from agentic_helpers import as_user, failure, register_tools
from httpx import ConnectError, Response
from tools import health, metrics

PROM_URL = f"{health.PROMETHEUS_URL}/api/v1/query"
ALERTS_URL = f"{health.PROMETHEUS_URL}/api/v1/alerts"
NOW = time.time()


def alert(name, severity, component, summary=None, active_at=None, **labels):
    """One firing alert as Prometheus's /api/v1/alerts reports it."""
    when = datetime.fromtimestamp(active_at or NOW, tz=timezone.utc)
    return {
        "labels": {
            "alertname": name,
            "severity": severity,
            "component": component,
            **labels,
        },
        "annotations": {"summary": summary or name},
        "state": "firing",
        "activeAt": when.strftime("%Y-%m-%dT%H:%M:%S.123456789Z"),
        "value": "1",
    }


def resolved(name):
    """One alert of the 6-hour history query."""
    return {"metric": {"alertname": name}, "value": [NOW, "1"]}


def mock_prometheus(
    firing=(), recent=(), home_util=0.41, home_kb=25 * 1024 * 1024, failing=()
):
    """Route the alerts endpoint and each instant query to a canned result;
    a query containing a substring of `failing` answers 503."""
    respx.get(ALERTS_URL).respond(200, json={"data": {"alerts": list(firing)}})

    def responder(request):
        query = request.url.params.get("query", "")
        if any(part in query for part in failing):
            return Response(503)
        if "ALERTS" in query:
            result = list(recent)
        elif "af_home_dir_util" in query:
            result = [{"value": [NOW, str(home_util)]}] if home_util is not None else []
        else:
            assert "af_home_dir_size_kb" in query
            result = [{"value": [NOW, str(home_kb)]}] if home_kb is not None else []
        return Response(200, json={"data": {"result": result}})

    respx.get(PROM_URL).mock(side_effect=responder)


async def run(**kwargs):
    tools = register_tools(health)
    with respx.mock:
        mock_prometheus(**kwargs)
        return await tools.tools["get_facility_health"]()


async def test_healthy_when_nothing_is_firing(user_ctx):
    out = await run()
    assert "**Healthy**" in out
    assert "Nothing is failing" in out


async def test_warning_alone_does_not_degrade_the_facility(user_ctx):
    """A warning must not turn the headline to Degraded, or it stops meaning
    anything — but it also must not claim Healthy when storage is slow."""
    out = await run(firing=[alert("AFMountHealthUnknown", "warning", "data")])
    assert "**Degraded**" not in out


async def test_unknown_storage_is_not_reported_as_healthy(user_ctx):
    out = await run(
        firing=[
            alert(
                "AFMountHealthUnknown",
                "warning",
                "data",
                summary="Data access unknown for eos on node n1",
            )
        ]
    )
    assert "**Partly unknown**" in out
    assert "Data access unknown for eos on node n1" in out


async def test_mount_slow_is_impaired_not_healthy(user_ctx):
    """Elevated metadata latency is felt by users, so it is never Healthy."""
    many = [
        alert(
            "AFMountSlow",
            "warning",
            "data",
            summary=f"eos is slow on node n{i}",
            mount_name="eos",
            node=f"n{i}",
        )
        for i in range(14)
    ]
    out = await run(firing=many)
    assert "**Impaired**" in out
    assert "**Healthy**" not in out
    assert "**Degraded**" not in out
    assert "Nothing is failing" not in out
    assert "**Data access**" in out
    # the rule's own summary, plus the scale the summary cannot express
    assert "eos is slow on node n0" in out
    assert "and 13 more, 14 nodes affected" in out


async def test_own_quota_warning_does_not_impair_the_facility(user_ctx):
    """Home quota is already reported as a percent; it must not flip the
    facility headline."""
    out = await run(
        firing=[alert("AFHomeDirUtilHigh", "warning", "storage", username="alice")],
        home_util=0.93,
    )
    assert "**Healthy**" in out
    assert "**Impaired**" not in out


@pytest.mark.parametrize(
    "name, severity",
    [("AFHomeDirUtilVeryHigh", "error"), ("AFHomeDirUtilCritical", "critical")],
)
async def test_own_quota_alert_is_not_a_facility_problem(user_ctx, name, severity):
    out = await run(firing=[alert(name, severity, "storage", username="alice")])
    assert "**Healthy**" in out
    assert "**Degraded**" not in out
    assert "Your home directory is at 41%" in out


async def test_an_ownerless_storage_alert_still_counts(user_ctx):
    out = await run(firing=[alert("AFSharedVolumeFull", "error", "storage")])
    assert "**Degraded**" in out
    assert "**User storage**: AFSharedVolumeFull." in out


async def test_an_operators_alert_is_invisible_to_users(user_ctx):
    out = await run(
        firing=[
            alert(
                "AFLokiStorageWillFill",
                "warning",
                "observability",
                summary="Loki PVC is on track to fill within 7 days",
                audience="operators",
            )
        ]
    )
    assert "**Healthy**" in out
    assert "Loki" not in out


async def test_a_log_store_outage_reads_in_user_terms(user_ctx):
    out = await run(firing=[alert("AFLokiDown", "error", "observability", "No logs")])
    assert "**Degraded**" in out
    assert "**Logs and monitoring**: No logs." in out
    assert "observability" not in out


async def test_error_degrades_the_facility(user_ctx):
    out = await run(firing=[alert("AFHubDown", "critical", "access")])
    assert "**Degraded**" in out


async def test_mount_invalid_is_impaired_not_degraded(user_ctx):
    """Mount failures are often transient EOS/NFS blips — warn, don't page
    the facility as Degraded."""
    out = await run(
        firing=[
            alert(
                "AFMountInvalid",
                "warning",
                "data",
                summary="eos is failing on node n1",
                mount_name="eos",
            )
        ]
    )
    assert "**Impaired**" in out
    assert "**Degraded**" not in out
    assert "**Data access**: eos is failing on node n1." in out
    assert "more" not in out  # a single instance carries no scale suffix


async def test_not_ready_workers_degrade_compute_capacity(user_ctx):
    """Offline AF nodes must show under compute — not look like healthy storage."""
    many = [
        alert(
            "AFProdNodesNotReady",
            "error",
            "compute",
            summary=f"AF worker node paf-{i} is NotReady",
            node=f"paf-{i}",
        )
        for i in range(9)
    ]
    out = await run(firing=many)
    assert "**Degraded**" in out
    assert "**Compute capacity**: AF worker node paf-0 is NotReady" in out
    assert "and 8 more, 9 nodes affected" in out
    assert "facility access, data access" in out.lower()
    assert "compute capacity" not in out.lower().split("normal:")[-1]


async def test_never_reports_another_users_alert(user_ctx):
    """af-pod-monitor alerts carry a username; only the caller's own may show."""
    out = await run(
        firing=[
            alert("AFHomeDirUtilHigh", "warning", "storage", username="someone-else")
        ]
    )
    assert "someone-else" not in out
    assert "AFHomeDirUtilHigh" not in out
    assert "**Healthy**" in out


async def test_a_hidden_alert_is_not_counted_as_resolved(user_ctx):
    """It is still firing — hiding it from this user must not turn it into
    "came and went"."""
    hidden = alert("AFHomeDirUtilHigh", "warning", "storage", username="someone-else")
    out = await run(firing=[hidden], recent=[resolved("AFHomeDirUtilHigh")])
    assert "came and went" not in out


async def test_resolved_alerts_are_counted(user_ctx):
    out = await run(recent=[resolved("AFMountSlow")])
    assert "In the last 6 hours, 1 issue came and went." in out


async def test_times_are_eastern_and_labelled(user_ctx):
    out = await run(
        firing=[
            alert("AFMountInvalid", "warning", "data", active_at=NOW - 7200),
            alert("AFMountInvalid", "warning", "data", active_at=NOW - 60),
        ]
    )
    assert " ET, " in out
    assert "(2h 00m ago)" in out  # the earliest instance dates the problem


async def test_an_undatable_alert_is_still_reported(user_ctx):
    missing = alert("AFMountInvalid", "warning", "data", summary="Depot is broken")
    del missing["activeAt"]
    garbled = alert("AFMountInvalid", "warning", "data")
    garbled["activeAt"] = "yesterday"

    out = await run(firing=[missing, garbled])

    assert "Depot is broken (and 1 more)." in out
    assert "Since" not in out


async def test_reports_the_callers_own_quota(user_ctx):
    out = await run(
        firing=[alert("AFHomeDirUtilHigh", "warning", "storage", username="alice")],
        home_util=0.93,
    )
    assert "93%" in out
    assert "Approaching the limit" in out


async def test_the_limit_note_follows_the_alert_rule_not_a_number(user_ctx):
    out = await run(home_util=0.93)
    assert "93%" in out
    assert "Approaching the limit" not in out


async def test_another_users_alert_does_not_warn_the_caller(user_ctx):
    out = await run(
        firing=[
            alert("AFHomeDirUtilCritical", "critical", "storage", username="someone")
        ]
    )
    assert "**Healthy**" in out
    assert "Approaching the limit" not in out


async def test_a_failed_history_read_draws_no_conclusion(user_ctx):
    out = await run(
        firing=[alert("AFHubDown", "critical", "access")], failing=["ALERTS"]
    )
    assert "**Degraded**" in out
    assert "alert history could not be read" in out
    assert "steady" not in out
    assert "came and went" not in out


async def test_a_failed_quota_read_is_not_a_stopped_session(user_ctx):
    out = await run(failing=["af_home_dir_util"])
    assert "quota could not be read" in out
    assert "No reading" not in out
    assert "no session" not in out


async def test_a_failed_size_read_prints_no_size(user_ctx):
    out = await run(home_util=0.5, failing=["af_home_dir_size_kb"])
    assert "Your home directory is at 50% of its quota." in out
    assert " GB" not in out


async def test_a_missing_size_prints_no_size(user_ctx):
    out = await run(home_util=0.5, home_kb=None)
    assert "Your home directory is at 50% of its quota." in out


def test_history_counts_alerts_that_fired_once_and_keeps_everyones_scope():
    """A firing alert's ALERTS series exists only while it fires, so a single
    episode counts; ALERTS_FOR_STATE's value stays constant through one."""
    query = health._HISTORY_QUERY
    assert query.startswith("count by (alertname) (count_over_time(ALERTS{")
    assert 'alertstate="firing"' in query
    assert 'component!~"delivery|dev"' in query
    assert 'audience!="operators"' in query
    assert 'username=""' in query
    assert 'node_pool!="dev"' in query


async def test_monitoring_outage_is_not_a_health_claim(user_ctx):
    """If Prometheus cannot be reached, say so rather than implying healthy."""
    tools = register_tools(health)
    with respx.mock:
        respx.get(ALERTS_URL).mock(side_effect=ConnectError("down"))
        out = await failure(tools.tools["get_facility_health"]())

    assert out.startswith("Error: Prometheus unreachable")
    assert "cannot tell you" in out
    assert "**Healthy**" not in out


async def test_monitoring_http_error_is_reported_with_its_reason(user_ctx):
    tools = register_tools(health)
    with respx.mock:
        respx.get(ALERTS_URL).respond(503, json={"error": "query engine overloaded"})
        out = await failure(tools.tools["get_facility_health"]())

    assert "returned HTTP 503 while trying to read the firing alerts" in out
    assert "— query engine overloaded" in out
    assert "cannot tell you" in out


async def test_malformed_alert_list_is_reported(user_ctx):
    tools = register_tools(health)
    with respx.mock:
        respx.get(ALERTS_URL).respond(200, text="<html>login</html>")
        out = await failure(tools.tools["get_facility_health"]())
    assert "was not an alert list" in out
    assert "cannot tell you" in out


async def test_healthy_without_a_running_session_is_still_healthy(user_ctx):
    """No quota reading means no session — not a monitoring outage."""
    out = await run(home_util=None)
    assert "**Healthy**" in out
    assert "cannot tell you" not in out
    assert "No reading of your home directory quota" in out


async def test_output_avoids_internal_mechanics(user_ctx):
    """Users get facility facts, not the monitoring implementation."""
    out = await run(
        firing=[
            alert("AFMountInvalid", "warning", "data", summary="eos is failing on n1")
        ]
    )
    for jargon in ("probe", "Prometheus", "af_node_mount", "kubectl", "Job"):
        assert jargon not in out


async def test_quota_query_escapes_quotes_in_username():
    """A crafted username cannot break out of the PromQL label matcher."""
    captured: list[str] = []

    def responder(request):
        captured.append(request.url.params.get("query", ""))
        return Response(200, json={"data": {"result": []}})

    with as_user(username='al"ice'):
        tools = register_tools(health)
        with respx.mock:
            respx.get(ALERTS_URL).respond(200, json={"data": {"alerts": []}})
            respx.get(PROM_URL).mock(side_effect=responder)
            await tools.tools["get_facility_health"]()

    quota_queries = [q for q in captured if "af_home_dir" in q]
    assert quota_queries
    for q in quota_queries:
        assert 'username="al\\"ice"' in q


async def test_dev_hardware_is_invisible_to_users(user_ctx):
    """Dev nodes are monitored for operators but run no user sessions; a user
    asking about the facility must not see them, and they must not degrade it."""
    out = await run(
        firing=[
            alert("AFMountInvalidDev", "warning", "dev", node="a337"),
            alert("AFMountSlow", "warning", "data", node_pool="dev", node="a338"),
        ]
    )
    assert "**Healthy**" in out
    assert "a337" not in out
    assert "a338" not in out
    assert "dev" not in out.lower().split("normal:")[-1]


async def test_a_failed_delivery_is_invisible_to_users(user_ctx):
    """A failing release leaves the workloads running, so it is an operator's
    alert and must not turn every user's answer to Impaired."""
    out = await run(firing=[alert("AFHelmReleaseNotReady", "warning", "delivery")])
    assert "**Healthy**" in out
    assert "AFHelmReleaseNotReady" not in out


class TestDashboardComponents:
    """Rows of the dashboard's Components panel, which may have no alert."""

    @pytest.fixture
    def dashboard(self, tmp_path, monkeypatch):
        targets = [
            {"expr": "geddes", "legendFormat": "Geddes storage"},
            {"expr": "work", "legendFormat": "/work"},
            {"expr": "hub", "legendFormat": "JupyterHub"},
        ]
        panel = {
            "title": "Components",
            "datasource": {"uid": "prometheus-rancher"},
            "targets": targets,
        }
        path = tmp_path / "default.json"
        path.write_text(json.dumps({"panels": [panel]}))
        monkeypatch.setattr(metrics, "DASHBOARD_PATH", str(path))

    def rows(self, values):
        def answer(request):
            query = dict(request.url.params)["query"]
            if values.get(query, 0) is None:
                return Response(503)
            result = []
            if query in values:
                result = [{"metric": {}, "value": [0, str(values[query])]}]
            return Response(200, json={"data": {"result": result}})

        return answer

    async def report(self, values, *alerts):
        tools = register_tools(health)
        with as_user():
            with respx.mock:
                respx.get(ALERTS_URL).mock(
                    return_value=Response(200, json={"data": {"alerts": list(alerts)}})
                )
                respx.get(PROM_URL).mock(side_effect=self.rows({}))
                respx.get(f"{metrics.CLUSTER_PROMETHEUS_URL}/api/v1/query").mock(
                    side_effect=self.rows(values)
                )
                return await tools.tools["get_facility_health"]()

    async def test_a_row_at_zero_with_no_alert_is_degraded(self, dashboard):
        out = await self.report({"geddes": 0, "work": 1, "hub": 1})
        assert "**Degraded**" in out
        assert "**Geddes storage** is down" in out
        assert "Nothing is failing" not in out

    async def test_a_partial_row_with_no_alert_is_impaired(self, dashboard):
        out = await self.report({"geddes": 0.5, "work": 1, "hub": 1})
        assert "**Impaired**" in out
        assert "**Geddes storage** is at 50%" in out

    async def test_a_row_an_alert_covers_is_not_said_twice(self, dashboard):
        firing = alert(
            "AFMountFailing", "warning", "data", "/work is failing on node a"
        )
        out = await self.report({"geddes": 1, "work": 0.95, "hub": 1}, firing)
        assert "/work is failing on node a" in out
        assert "facility dashboard" not in out

    async def test_a_row_that_cannot_be_read_is_unknown_not_ok(self, dashboard):
        out = await self.report({"work": 1, "hub": 1, "geddes": None})
        assert "**Partly unknown**" in out
        assert "Could not read: Geddes storage." in out
        assert "Nothing is failing" not in out

    async def test_no_normal_line_beside_a_row_in_trouble(self, dashboard):
        out = await self.report({"geddes": 0, "work": 1, "hub": 1})
        assert "Normal:" not in out

    async def test_all_rows_ok_is_healthy(self, dashboard):
        out = await self.report({"geddes": 1, "work": 1, "hub": 1})
        assert "**Healthy**" in out
        assert "Nothing is failing" in out

    async def test_a_blank_row_is_not_a_problem(self, dashboard):
        out = await self.report({"work": 1, "hub": 1})
        assert "**Healthy**" in out

    async def test_no_dashboard_leaves_the_report_to_the_alerts(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(metrics, "DASHBOARD_PATH", str(tmp_path / "none.json"))
        assert "**Healthy**" in await self.report({})
