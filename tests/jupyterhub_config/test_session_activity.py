"""Tests for extraFiles/session-activity.py — the hub's own last_activity on
/hub/metrics, which is what the dashboard's "Last active" column reads."""

import datetime
import types

import prometheus_client
import pytest
from common import ConfigSink
from hub_helpers import load_snippet
from prometheus_client import CollectorRegistry

UTC = datetime.timezone.utc
LAST = datetime.datetime(2026, 9, 8, 12, 0)  # naive UTC, as JupyterHub stores it
STARTED = datetime.datetime(2026, 9, 1, 9, 30)


@pytest.fixture
def registry(monkeypatch):
    """A private registry, so exec'ing the snippet does not collide with the
    process-wide one (or with a second test)."""
    reg = CollectorRegistry()
    monkeypatch.setattr(prometheus_client, "REGISTRY", reg)
    return reg


def snippet(monkeypatch, template=None):
    config = ConfigSink()
    if template is not None:
        config.KubeSpawner.pod_name_template = template
    return load_snippet("session-activity.py", monkeypatch, extra_globals={"c": config})


def fake_spawner(username="alice", userid=7, name="", last=LAST, started=STARTED):
    user = None if username is None else types.SimpleNamespace(name=username, id=userid)
    return types.SimpleNamespace(
        user=user, name=name, last_activity=last, started=started
    )


def collect(ns):
    return {
        family.name: family for family in ns["SessionActivityCollector"]().collect()
    }


# ── timestamps ────────────────────────────────────────────────────────────────


def test_to_timestamp_treats_naive_columns_as_utc(monkeypatch, registry):
    ns = snippet(monkeypatch)
    assert ns["to_timestamp"](LAST) == LAST.replace(tzinfo=UTC).timestamp()
    # already-aware values are left alone rather than shifted twice
    aware = LAST.replace(tzinfo=UTC)
    assert ns["to_timestamp"](aware) == aware.timestamp()
    assert ns["to_timestamp"](None) is None


# ── pod name ──────────────────────────────────────────────────────────────────


def test_pod_name_follows_the_spawner_template(monkeypatch, registry):
    ns = snippet(monkeypatch, template="purdue-af-{userid}")
    assert ns["pod_name"]("alice", 7, "") == "purdue-af-7"


def test_pod_name_defaults_when_the_chart_sets_no_template(monkeypatch, registry):
    ns = snippet(monkeypatch)
    assert ns["pod_name"]("alice", 7, "") == "purdue-af-7"


# ── sessions ──────────────────────────────────────────────────────────────────


def test_sessions_labels_each_running_server(monkeypatch, registry):
    ns = snippet(monkeypatch)
    ns["running_spawners"] = lambda: [
        fake_spawner("alice", 7),
        fake_spawner("bob-cern", 12, name="gpu"),
    ]

    rows = ns["sessions"]()

    assert [labels for labels, _, _ in rows] == [
        ["alice", "", "purdue-af-7"],
        ["bob-cern", "gpu", "purdue-af-12"],
    ]
    assert rows[0][1] == LAST.replace(tzinfo=UTC).timestamp()
    assert rows[0][2] == STARTED.replace(tzinfo=UTC).timestamp()


def test_a_session_that_has_reported_nothing_falls_back_to_its_spawn_time(
    monkeypatch, registry
):
    ns = snippet(monkeypatch)
    ns["running_spawners"] = lambda: [fake_spawner(last=None)]

    (_, last, started) = ns["sessions"]()[0]

    assert last == started == STARTED.replace(tzinfo=UTC).timestamp()


def test_sessions_skips_rows_the_orm_cannot_resolve(monkeypatch, registry):
    ns = snippet(monkeypatch)
    ns["running_spawners"] = lambda: [
        fake_spawner(username=None),  # spawner with no user row
        fake_spawner(last=None, started=None),  # never started
        fake_spawner("carol", 3),
    ]

    assert [labels for labels, _, _ in ns["sessions"]()] == [
        ["carol", "", "purdue-af-3"]
    ]


# ── collector ─────────────────────────────────────────────────────────────────


def test_collector_exports_both_gauges(monkeypatch, registry):
    ns = snippet(monkeypatch)
    ns["running_spawners"] = lambda: [fake_spawner("alice", 7)]

    families = collect(ns)

    activity = families["af_session_last_activity_seconds"]
    assert activity.samples[0].labels == {
        "username": "alice",
        "servername": "",
        "pod": "purdue-af-7",
    }
    assert activity.samples[0].value == LAST.replace(tzinfo=UTC).timestamp()
    started = families["af_session_started_seconds"]
    assert started.samples[0].value == STARTED.replace(tzinfo=UTC).timestamp()


def test_a_broken_collection_does_not_take_hub_metrics_down(monkeypatch, registry):
    """/hub/metrics renders the whole registry in one pass, so raising here
    would lose every JupyterHub metric, not just this one."""
    ns = snippet(monkeypatch)

    def boom():
        raise RuntimeError("no database")

    ns["running_spawners"] = boom

    families = collect(ns)

    assert set(families) == {
        "af_session_last_activity_seconds",
        "af_session_started_seconds",
    }
    assert families["af_session_last_activity_seconds"].samples == []


def test_snippet_registers_itself_on_import(monkeypatch, registry):
    """Exec'ing the snippet is the whole deployment: z2jh runs it, and the
    metric has to be on the registry /hub/metrics renders."""
    snippet(monkeypatch)

    exported = {family.name for family in registry.collect()}

    assert "af_session_last_activity_seconds" in exported
    assert "af_session_started_seconds" in exported
