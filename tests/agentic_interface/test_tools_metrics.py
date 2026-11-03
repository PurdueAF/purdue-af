"""Tests for tools/metrics.py: the public dashboard's numbers. What matters most
is that only the dashboard's own queries run, and that no result with a label
outside the allowlist reaches the output, whatever the panel."""

import json
import re

import pytest
import respx
from agentic_helpers import failure, register_tools
from common import REPO
from httpx import ConnectError, Response
from tools import metrics

PROM = f"{metrics.PROMETHEUS_URL}/api/v1/query"
RANCHER = f"{metrics.CLUSTER_PROMETHEUS_URL}/api/v1/query"
AF = {"uid": "prometheus"}


def panel(title, expr, legend="", datasource=AF, unit=None, **target):
    return {
        "title": title,
        "type": "timeseries",
        "datasource": datasource,
        "fieldConfig": {"defaults": {"unit": unit}},
        "targets": [{"expr": expr, "legendFormat": legend, **target}],
    }


DASHBOARD = {
    "panels": [
        {"type": "row", "title": "Status", "panels": []},
        panel("Sessions", "sessions"),
        panel("Storage", "storage", unit="percentunit"),
        panel("Home", "home_by_user"),
        panel("Rate", "rate(x[$__range])"),
        panel("Ray on NRP", "nrp", datasource={"uid": "prometheus-nrp"}),
        panel("Hidden", "hidden", hide=True),
        panel("Cores", "cores", datasource={"uid": "prometheus-rancher"}),
        {"type": "text", "title": "Note"},
        {
            "type": "row",
            "title": "Nodes",
            "collapsed": True,
            "panels": [panel("Free", "free", "{{ node }}")],
        },
    ]
}


def sample(value, **labels):
    return {"metric": labels, "value": [0, str(value)]}


def answer(request, rows):
    query = dict(request.url.params)["query"]
    return Response(
        200,
        json={"data": {"result": rows.get(query, [])}},
    )


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    path = tmp_path / "default.json"
    path.write_text(json.dumps(DASHBOARD))
    monkeypatch.setattr(metrics, "DASHBOARD_PATH", str(path))


@pytest.fixture
def tool(dashboard):
    return register_tools(metrics).tools["get_facility_metrics"]


async def test_no_section_lists_them(tool):
    out = await tool()
    assert "- Status" in out and "- Nodes" in out


async def test_an_unknown_section_names_the_real_ones(tool):
    message = await failure(tool(section="Nope"))
    assert "Status" in message


@respx.mock
async def test_a_section_gives_its_panels_values(tool):
    rows = {
        "sessions": [sample(12)],
        "storage": [sample(0.5)],
        "rate(x[1h])": [sample(1.25)],
        "cores": [sample(40)],
        "home_by_user": [],
    }
    respx.get(PROM).mock(side_effect=lambda r: answer(r, rows))
    respx.get(RANCHER).mock(side_effect=lambda r: answer(r, rows))
    out = await tool(section="status")
    assert "- Sessions: 12" in out
    assert "- Storage: 50%" in out
    assert "- Rate: 1.25" in out
    assert "- Cores: 40" in out
    assert "hidden" not in out.lower()


@respx.mock
async def test_only_the_dashboards_queries_run(tool):
    asked = []

    def record(request):
        asked.append(dict(request.url.params)["query"])
        return Response(200, json={"data": {"result": []}})

    respx.get(PROM).mock(side_effect=record)
    respx.get(RANCHER).mock(side_effect=record)
    await tool(section="Status")
    assert sorted(asked) == sorted(
        ["sessions", "storage", "home_by_user", "rate(x[1h])", "cores"]
    )


@respx.mock
async def test_a_series_with_a_label_not_allowed_is_never_shown(tool):
    rows = {
        "sessions": [sample(1)],
        "home_by_user": [
            sample(0.9, node="n1"),
            sample(0.8, username="alice", pod="purdue-af-1"),
        ],
    }
    respx.get(PROM).mock(side_effect=lambda r: answer(r, rows))
    respx.get(RANCHER).mock(side_effect=lambda r: answer(r, {}))
    out = await tool(section="Status")
    assert "Home" not in out
    assert "alice" not in out and "purdue-af-1" not in out and "n1" not in out


@respx.mock
async def test_instance_addresses_are_dropped(tool):
    rows = {"sessions": [sample(3, instance="10.0.0.1:9090", job="x")]}
    respx.get(PROM).mock(side_effect=lambda r: answer(r, rows))
    respx.get(RANCHER).mock(side_effect=lambda r: answer(r, {}))
    out = await tool(section="Status")
    assert "- Sessions: 3" in out
    assert "10.0.0.1" not in out


@pytest.mark.parametrize(
    ("value", "shown"), [(1, "OK"), (0.95, "partial"), (0, "down"), (7, "OK")]
)
def test_a_status_row_shows_the_dashboards_word_not_a_number(value, shown):
    mappings = [
        {"type": "range", "options": {"from": 1, "to": 1e9, "result": {"text": "OK"}}},
        {
            "type": "range",
            "options": {"from": 0.0001, "to": 0.9999, "result": {"text": "partial"}},
        },
        {"type": "range", "options": {"from": -1, "to": 0, "result": {"text": "down"}}},
    ]
    assert metrics._value(value, None, mappings) == shown


def test_a_value_mapping_names_its_value():
    mappings = [{"type": "value", "options": {"1": {"text": "healthy"}}}]
    assert metrics._value(1.0, None, mappings) == "healthy"
    assert metrics._value(2.0, None, mappings) == "2"


@respx.mock
async def test_a_series_shows_the_word_its_own_override_gives(dashboard, monkeypatch):
    status = panel("Sync", "daemon", "daemon")
    status["fieldConfig"]["overrides"] = [
        {
            "matcher": {"id": "byName", "options": "daemon"},
            "properties": [
                {
                    "id": "mappings",
                    "value": [
                        {"type": "value", "options": {"1": {"text": "alive"}}},
                        {"type": "value", "options": {"0": {"text": "STALLED"}}},
                    ],
                }
            ],
        }
    ]
    path = metrics.DASHBOARD_PATH
    with open(path, "w") as out:
        json.dump({"panels": [{"type": "row", "title": "Pixi"}, status]}, out)
    respx.get(PROM).mock(
        return_value=Response(200, json={"data": {"result": [sample(0)]}})
    )
    tool = register_tools(metrics).tools["get_facility_metrics"]
    assert "- Sync: daemon: STALLED" in await tool(section="Pixi")


@respx.mock
async def test_the_real_components_rows_read_as_words(monkeypatch):
    path = REPO / "apps" / "monitoring" / "grafana" / "dashboards" / "default.json"
    monkeypatch.setattr(metrics, "DASHBOARD_PATH", str(path))
    tool = register_tools(metrics).tools["get_facility_metrics"]
    rows = [sample(0.95)]
    respx.get(PROM).mock(return_value=Response(200, json={"data": {"result": rows}}))
    respx.get(RANCHER).mock(return_value=Response(200, json={"data": {"result": rows}}))
    section = next(
        name
        for name, panels in metrics.sections().items()
        if any(p.get("title") == metrics.COMPONENTS_PANEL for p in panels)
    )
    out = await tool(section=section)
    block = out.split("- Components:")[1].split("\n- ")[0]
    assert "partial" in block
    assert "0.95" not in block and "%" not in block


@respx.mock
async def test_allowed_labels_name_the_series(tool):
    rows = {"free": [sample(4, node="a"), sample(0, node="b")]}
    respx.get(PROM).mock(side_effect=lambda r: answer(r, rows))
    out = await tool(section="Nodes")
    assert "  - a: 4" in out and "  - b: 0" in out


@respx.mock
async def test_a_ratio_with_no_denominator_is_left_out(tool):
    rows = {"free": [sample("NaN", node="a"), sample(2, node="b")]}
    respx.get(PROM).mock(side_effect=lambda r: answer(r, rows))
    out = await tool(section="Nodes")
    assert "- Free: b: 2" in out
    assert "nan" not in out.lower()


@respx.mock
async def test_a_panels_series_are_capped(tool):
    count = metrics.MAX_SERIES + 5
    rows = {"free": [sample(1, node=f"n{i}") for i in range(count)]}
    respx.get(PROM).mock(side_effect=lambda r: answer(r, rows))
    assert "and 5 more" in await tool(section="Nodes")


@respx.mock
async def test_some_failed_panels_are_said_to_be_missing(tool):
    def flaky(request):
        if dict(request.url.params)["query"] == "sessions":
            return Response(503)
        return answer(request, {"storage": [sample(0.1)]})

    respx.get(PROM).mock(side_effect=flaky)
    respx.get(RANCHER).mock(side_effect=lambda r: answer(r, {}))
    out = await tool(section="Status")
    assert "- Storage: 10%" in out
    assert "could not be read" in out


@respx.mock
async def test_a_down_monitoring_system_is_an_error_not_empty(tool):
    respx.get(PROM).mock(side_effect=ConnectError("down"))
    respx.get(RANCHER).mock(side_effect=ConnectError("down"))
    assert "unreachable" in await failure(tool(section="Status"))


@respx.mock
async def test_the_real_dashboard_never_shows_a_person_or_a_pod(monkeypatch):
    path = REPO / "apps" / "monitoring" / "grafana" / "dashboards" / "default.json"
    monkeypatch.setattr(metrics, "DASHBOARD_PATH", str(path))
    tool = register_tools(metrics).tools["get_facility_metrics"]

    def labelled(request):
        """Each query's `by (...)` labels: an allowed one holds a harmless
        value, any other a name that must never come out."""
        query = dict(request.url.params)["query"]
        grouped = re.findall(r"by\s*\(([^)]*)\)", query)
        names = {n.strip() for g in grouped for n in g.split(",") if n.strip()}
        labels = {
            name: "fine" if name in metrics.ALLOWED_LABELS else "alice"
            for name in names
        }
        return Response(200, json={"data": {"result": [sample(1, **labels)]}})

    respx.get(PROM).mock(side_effect=labelled)
    respx.get(RANCHER).mock(side_effect=labelled)
    sections = metrics.sections()
    assert len(sections) > 3
    shown = 0
    for section in sections:
        out = await tool(section=section)
        assert "alice" not in out
        shown += out.count("\n")
    assert shown > 20
    panels = [p for members in sections.values() for p in members]
    for panel_ in panels:
        for _, query, _ in metrics._queries(panel_):
            assert "$__" not in query
