"""Tests for docker/af-node-monitor/node_healthcheck.py.

The kubernetes client is faked at module-global level (the real package is
not installed in this suite), so the orchestration logic — job creation
throttling, cleanup retention policies, metric decision matrix — is tested
without a cluster.
"""

import json
import time
import types
from datetime import datetime, timezone

import node_healthcheck as nh
import pytest
from prometheus_client import REGISTRY

NOW = time.time()


# ── fakes ─────────────────────────────────────────────────────────────────────


def fake_node(name, ready=True):
    cond = types.SimpleNamespace(type="Ready", status="True" if ready else "False")
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name),
        status=types.SimpleNamespace(conditions=[cond]),
    )


def fake_job(
    name="job-1",
    labels=None,
    active=0,
    succeeded=0,
    failed=0,
    started_ago=None,
    finished_ago=None,
):
    def ts(seconds_ago):
        if seconds_ago is None:
            return None
        return datetime.fromtimestamp(NOW - seconds_ago, tz=timezone.utc)

    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, labels=labels or {}),
        status=types.SimpleNamespace(
            active=active,
            succeeded=succeeded,
            failed=failed,
            conditions=[],
            start_time=ts(started_ago),
            completion_time=ts(finished_ago),
        ),
    )


class FakeCoreV1:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = 0

    def list_node(self, label_selector):
        self.calls += 1
        return types.SimpleNamespace(items=self.nodes)


class FakeBatchV1:
    def __init__(self, jobs=None):
        self.jobs = jobs or []
        self.created = []
        self.deleted = []

    def list_namespaced_job(self, namespace, label_selector=None):
        return types.SimpleNamespace(items=self.jobs)

    def create_namespaced_job(self, namespace, body):
        self.created.append(body)

    def delete_namespaced_job(self, name, namespace, propagation_policy, body=None):
        self.deleted.append(name)


@pytest.fixture
def k8s(monkeypatch):
    """Wire fake k8s clients into the module and reset its mutable state."""
    core = FakeCoreV1(nodes=[fake_node("node-a")])
    batch = FakeBatchV1()
    monkeypatch.setattr(nh, "_init_k8s", lambda: None)
    monkeypatch.setattr(nh, "_k8s_ready", True)
    # _core_v1/_batch_v1 are annotation-only declarations until _init_k8s runs
    monkeypatch.setattr(nh, "_core_v1", core, raising=False)
    monkeypatch.setattr(nh, "_batch_v1", batch, raising=False)
    monkeypatch.setattr(
        nh, "client", types.SimpleNamespace(V1DeleteOptions=lambda **kw: kw)
    )
    monkeypatch.setattr(nh, "_node_cache", [])
    monkeypatch.setattr(nh, "_last_node_refresh", 0.0)
    nh._last_job_start_ts.clear()
    return types.SimpleNamespace(core=core, batch=batch)


@pytest.fixture(autouse=True)
def clear_metrics():
    for metric in (
        nh.mount_valid,
        nh.mount_ping_ms,
        nh.mount_data_rate_gbps,
        nh.mount_metadata_latency_ms,
        nh.mount_timeout_total,
        nh.mount_last_success_ts,
    ):
        metric.clear()
    yield


def sample(name, mount="/depot/", node="node-a", **extra):
    labels = {
        "mount_name": mount,
        "mount_path": nh.MOUNTS[mount]["mount_path"],
        "node": node,
        **extra,
    }
    return REGISTRY.get_sample_value(name, labels)


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_result_path_naming(monkeypatch, tmp_path):
    monkeypatch.setattr(nh, "RESULTS_DIR", tmp_path)
    assert nh._result_path("/depot/", "node-a") == tmp_path / "depot__node-a.json"
    assert nh._result_path("/depot/", "") == tmp_path / "depot.json"


def test_load_result_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(nh, "RESULTS_DIR", tmp_path)
    (tmp_path / "depot__node-a.json").write_text('{"ok": true}')
    assert nh._load_result("/depot/", "node-a") == {"ok": True}


def test_load_result_missing_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(nh, "RESULTS_DIR", tmp_path)
    assert nh._load_result("/depot/", "node-a") is None


def test_load_result_corrupt_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(nh, "RESULTS_DIR", tmp_path)
    (tmp_path / "depot__node-a.json").write_text("{ nope")
    assert nh._load_result("/depot/", "node-a") is None


def test_load_result_storage_error_flagged(monkeypatch, tmp_path):
    monkeypatch.setattr(nh, "RESULTS_DIR", tmp_path)
    # a directory at the result path raises IsADirectoryError (an OSError)
    (tmp_path / "depot__node-a.json").mkdir()
    assert nh._load_result("/depot/", "node-a") == {"_storage_error": True}


def test_mount_job_env_full_config():
    cfg = nh.MOUNTS["/depot/"]
    env = {e["name"]: e.get("value") for e in nh._mount_job_env("/depot/", cfg)}
    assert env["MOUNT_NAME"] == "/depot/"
    assert env["CHECK_FILE"] == cfg["job"]["check_file"]
    assert env["CHECKSUM"] == cfg["job"]["checksum"]
    assert env["ENABLE_FIO"] == "true"
    assert "NODE_NAME" in env  # injected via fieldRef


def test_mount_job_env_fio_disabled():
    env = {
        e["name"]: e.get("value")
        for e in nh._mount_job_env("cvmfs", nh.MOUNTS["cvmfs"])
    }
    assert env["ENABLE_FIO"] == "false"
    assert "FIO_FILE" not in env


def test_build_job_manifest():
    body = nh._build_job_manifest("/depot/", nh.MOUNTS["/depot/"], "node-a")
    assert body["metadata"]["name"].startswith("af-node-monitor-depot-node-a-")
    assert body["metadata"]["labels"] == {
        "app": "af-node-monitor",
        "mount": "depot",
        "node": "node-a",
    }
    pod = body["spec"]["template"]["spec"]
    assert pod["nodeName"] == "node-a"
    assert pod["containers"][0]["image"] == nh.JOB_IMAGE
    assert body["spec"]["ttlSecondsAfterFinished"] == nh.JOB_TTL_SECONDS
    assert pod["volumes"] == nh.MOUNTS["/depot/"]["volumes"]


# ── node discovery ────────────────────────────────────────────────────────────


def test_list_target_nodes_filters_not_ready(k8s):
    k8s.core.nodes = [fake_node("ready-1"), fake_node("broken", ready=False)]
    assert nh._list_target_nodes() == ["ready-1"]


def test_list_target_nodes_is_cached(k8s):
    nh._list_target_nodes()
    first_calls = k8s.core.calls
    nh._list_target_nodes()
    assert k8s.core.calls == first_calls  # served from cache


def test_list_target_nodes_without_k8s(monkeypatch):
    monkeypatch.setattr(nh, "_k8s_ready", False)
    monkeypatch.setattr(nh, "_init_k8s", lambda: None)
    assert nh._list_target_nodes() == []


# ── job orchestration ─────────────────────────────────────────────────────────


def test_ensure_jobs_creates_one_per_mount_node(k8s):
    nh._ensure_jobs(NOW)
    created_mounts = {b["metadata"]["labels"]["mount"] for b in k8s.batch.created}
    assert created_mounts == {"depot", "work", "eos", "cvmfs"}


def test_ensure_jobs_throttled_by_interval(k8s):
    nh._ensure_jobs(NOW)
    n = len(k8s.batch.created)
    nh._ensure_jobs(NOW + 1)  # well within JOB_INTERVAL_S
    assert len(k8s.batch.created) == n


def test_ensure_jobs_skips_active(k8s):
    k8s.batch.jobs = [fake_job(active=1)]
    nh._ensure_jobs(NOW)
    assert k8s.batch.created == []


def test_active_job_keys(k8s):
    k8s.batch.jobs = [
        fake_job(labels={"mount": "depot", "node": "node-a"}, active=1),
        fake_job(labels={"mount": "work", "node": "node-a"}, active=0),
        fake_job(labels={}, active=1),  # unlabeled: ignored
    ]
    assert nh._list_active_job_keys() == {("depot", "node-a")}


def test_cleanup_deletes_succeeded_immediately(k8s):
    k8s.batch.jobs = [fake_job(name="done", succeeded=1, finished_ago=1)]
    nh._cleanup_finished_jobs(NOW)
    assert k8s.batch.deleted == ["done"]


def test_cleanup_retains_recent_failures(k8s, monkeypatch):
    monkeypatch.setattr(nh, "JOB_FAILED_RETENTION_S", 60.0)
    k8s.batch.jobs = [fake_job(name="crashed", failed=1, finished_ago=5)]
    nh._cleanup_finished_jobs(NOW)
    assert k8s.batch.deleted == []

    k8s.batch.jobs = [fake_job(name="crashed", failed=1, finished_ago=120)]
    nh._cleanup_finished_jobs(NOW)
    assert k8s.batch.deleted == ["crashed"]


def test_cleanup_force_kills_overrunning_jobs(k8s, monkeypatch):
    monkeypatch.setattr(nh, "JOB_MAX_RUNTIME_S", 300.0)
    k8s.batch.jobs = [fake_job(name="stuck", active=1, started_ago=400)]
    nh._cleanup_finished_jobs(NOW)
    assert k8s.batch.deleted == ["stuck"]


def test_cleanup_leaves_running_jobs_alone(k8s):
    k8s.batch.jobs = [fake_job(name="busy", active=1, started_ago=10)]
    nh._cleanup_finished_jobs(NOW)
    assert k8s.batch.deleted == []


# ── update_metrics decision matrix ────────────────────────────────────────────


@pytest.fixture
def metrics_env(monkeypatch, tmp_path):
    """update_metrics with orchestration stubbed and results in tmp_path."""
    monkeypatch.setattr(nh, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(nh, "_ensure_jobs", lambda now: None)
    monkeypatch.setattr(nh, "_cleanup_finished_jobs", lambda now: None)
    monkeypatch.setattr(nh, "_list_target_nodes", lambda: ["node-a"])
    monkeypatch.setattr(nh, "_list_active_job_keys", lambda: set())

    def write(_mount, _node, **data):
        nh._result_path(_mount, _node).write_text(json.dumps(data))

    return write


def test_fresh_ok_result(metrics_env):
    metrics_env(
        "/depot/",
        "node-a",
        ok=True,
        timestamp=time.time(),
        ping_ms=1.5,
        metadata_ms=20.0,
        throughput_gbps=8.2,
    )

    nh.update_metrics()

    assert sample("af_node_mount_valid") == 1
    assert sample("af_node_mount_ping_ms") == 1.5
    assert sample("af_node_mount_metadata_latency_ms") == 20.0
    assert sample("af_node_mount_data_rate_gbps") == 8.2
    assert sample("af_node_mount_last_success_timestamp_seconds") > 0


def test_missing_result_reports_timeout_semantics(metrics_env):
    nh.update_metrics()

    assert sample("af_node_mount_valid") == 0
    assert sample("af_node_mount_ping_ms") == nh._timeout_ping_ms()
    assert sample("af_node_mount_timeout_total", check_type="no_recent_result") == 1


def test_missing_result_with_active_job_is_never_started(metrics_env, monkeypatch):
    monkeypatch.setattr(nh, "_list_active_job_keys", lambda: {("depot", "node-a")})

    nh.update_metrics()

    assert sample("af_node_mount_timeout_total", check_type="job_never_started") == 1


def test_stale_result(metrics_env, monkeypatch):
    monkeypatch.setattr(nh, "RESULT_STALE_WINDOW_S", 100.0)
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time() - 1000)

    nh.update_metrics()

    assert sample("af_node_mount_valid") == 0
    assert sample("af_node_mount_timeout_total", check_type="stale_result") == 1


def test_timeout_result_uses_worst_case_latencies(metrics_env):
    metrics_env(
        "/depot/", "node-a", ok=True, timeout=True, timestamp=time.time(), ping_ms=2.0
    )

    nh.update_metrics()

    assert sample("af_node_mount_valid") == 0  # timeout invalidates ok
    assert sample("af_node_mount_ping_ms") == 2.0  # partial measurement kept
    assert sample("af_node_mount_metadata_latency_ms") == nh._timeout_metadata_ms()
    assert sample("af_node_mount_data_rate_gbps") == 0.0
    assert sample("af_node_mount_timeout_total", check_type="job_result") == 1


def test_storage_error_skips_metrics_entirely(metrics_env, tmp_path):
    nh._result_path("/depot/", "node-a").mkdir()  # triggers _storage_error

    nh.update_metrics()

    assert sample("af_node_mount_valid") is None  # nothing reported


def test_node_label_prefers_result_json(metrics_env):
    metrics_env(
        "/depot/",
        "node-a",
        ok=True,
        timestamp=time.time(),
        ping_ms=1.0,
        node="node-actual",
    )

    nh.update_metrics()

    assert sample("af_node_mount_valid", node="node-actual") == 1
    assert sample("af_node_mount_valid", node="node-a") is None


# ── remaining branches ────────────────────────────────────────────────────────


def test_vlog_and_elog(monkeypatch, capsys):
    monkeypatch.delenv("AF_NODE_MONITOR_VERBOSE", raising=False)
    nh._vlog("quiet")
    assert capsys.readouterr().out == ""

    monkeypatch.setenv("AF_NODE_MONITOR_VERBOSE", "true")
    nh._vlog("loud")
    assert "loud" in capsys.readouterr().out

    nh._elog("always")
    assert "always" in capsys.readouterr().out


def test_init_k8s_loads_config(monkeypatch):
    monkeypatch.setattr(nh, "_k8s_ready", False)
    monkeypatch.setattr(nh, "_core_v1", None, raising=False)
    monkeypatch.setattr(nh, "_batch_v1", None, raising=False)

    class FakeConfig:
        @staticmethod
        def load_incluster_config():
            raise Exception("not in cluster")

        @staticmethod
        def load_kube_config():
            return None

    class FakeCore:
        pass

    class FakeBatch:
        pass

    class FakeClient:
        CoreV1Api = FakeCore
        BatchV1Api = FakeBatch

    monkeypatch.setattr(nh, "config", FakeConfig)
    monkeypatch.setattr(nh, "client", FakeClient)
    monkeypatch.setenv("AF_NODE_MONITOR_VERBOSE", "1")

    nh._init_k8s()
    assert nh._k8s_ready is True
    assert isinstance(nh._core_v1, FakeCore)
    assert isinstance(nh._batch_v1, FakeBatch)

    # second call is a no-op once ready
    nh._init_k8s()


def test_list_target_nodes_skips_incomplete_and_api_errors(k8s, monkeypatch):
    nameless = types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=None),
        status=types.SimpleNamespace(conditions=[]),
    )
    no_conds = types.SimpleNamespace(
        metadata=types.SimpleNamespace(name="bare"),
        status=types.SimpleNamespace(conditions=None),
    )
    k8s.core.nodes = [nameless, no_conds, fake_node("ok")]
    assert nh._list_target_nodes() == ["ok"]

    monkeypatch.setattr(nh, "_node_cache", [])
    monkeypatch.setattr(nh, "_last_node_refresh", 0.0)

    def boom(label_selector):
        raise nh.ApiException("denied")

    k8s.core.list_node = boom
    assert nh._list_target_nodes() == []


def test_has_active_job_and_keys_without_k8s_or_on_error(monkeypatch, k8s):
    monkeypatch.setattr(nh, "_k8s_ready", False)
    assert nh._has_active_job("/depot/", "node-a") is False
    assert nh._list_active_job_keys() == set()

    monkeypatch.setattr(nh, "_k8s_ready", True)
    monkeypatch.setattr(nh, "_batch_v1", k8s.batch, raising=False)

    def boom(**kwargs):
        raise nh.ApiException("no")

    k8s.batch.list_namespaced_job = boom
    assert nh._has_active_job("/depot/", "node-a") is False
    assert nh._list_active_job_keys() == set()


def test_ensure_jobs_guards_and_create_failure(monkeypatch, k8s):
    monkeypatch.setattr(nh, "_k8s_ready", False)
    nh._ensure_jobs(NOW)
    assert k8s.batch.created == []

    monkeypatch.setattr(nh, "_k8s_ready", True)
    monkeypatch.setattr(nh, "_batch_v1", k8s.batch, raising=False)
    monkeypatch.setattr(nh, "_list_target_nodes", lambda: [])
    nh._ensure_jobs(NOW)
    assert k8s.batch.created == []

    monkeypatch.setattr(nh, "_list_target_nodes", lambda: ["node-a"])
    monkeypatch.setattr(nh, "_has_active_job", lambda *a: False)
    nh._last_job_start_ts.clear()

    def boom(**kwargs):
        raise nh.ApiException("create failed")

    k8s.batch.create_namespaced_job = boom
    nh._ensure_jobs(NOW)  # does not raise
    assert k8s.batch.created == []


def test_cleanup_guards_conditions_and_errors(monkeypatch, k8s):
    monkeypatch.setattr(nh, "_k8s_ready", False)
    nh._cleanup_finished_jobs(NOW)

    monkeypatch.setattr(nh, "_k8s_ready", True)
    monkeypatch.setattr(nh, "_batch_v1", k8s.batch, raising=False)

    def boom(**kwargs):
        raise nh.ApiException("list failed")

    k8s.batch.list_namespaced_job = boom
    nh._cleanup_finished_jobs(NOW)

    # restore list, exercise incomplete jobs + condition-based success/fail
    k8s.batch = FakeBatchV1()
    monkeypatch.setattr(nh, "_batch_v1", k8s.batch, raising=False)

    incomplete = fake_job(name="incomplete", finished_ago=None, started_ago=None)
    incomplete.status.completion_time = None
    incomplete.status.start_time = None
    incomplete.status.active = 0

    no_meta = types.SimpleNamespace(
        metadata=None,
        status=types.SimpleNamespace(active=0, succeeded=1, failed=0, conditions=[]),
    )

    via_cond = fake_job(name="via-cond", finished_ago=1)
    via_cond.status.succeeded = 0
    via_cond.status.failed = 0
    via_cond.status.conditions = [types.SimpleNamespace(type="Complete", status="True")]

    failed_cond = fake_job(name="fail-cond", finished_ago=120)
    failed_cond.status.succeeded = 0
    failed_cond.status.failed = 0
    failed_cond.status.conditions = [
        types.SimpleNamespace(type="Failed", status="True")
    ]

    k8s.batch.jobs = [incomplete, no_meta, via_cond, failed_cond]
    nh._cleanup_finished_jobs(NOW)
    assert "via-cond" in k8s.batch.deleted
    assert "fail-cond" in k8s.batch.deleted


def test_cleanup_force_delete_and_delete_api_errors(monkeypatch, k8s):
    monkeypatch.setattr(nh, "JOB_MAX_RUNTIME_S", 300.0)
    k8s.batch.jobs = [fake_job(name="stuck", active=1, started_ago=400)]

    def boom_delete(**kwargs):
        raise nh.ApiException("cannot delete")

    k8s.batch.delete_namespaced_job = boom_delete
    nh._cleanup_finished_jobs(NOW)  # swallows ApiException

    k8s.batch = FakeBatchV1(jobs=[fake_job(name="done", succeeded=1, finished_ago=1)])
    monkeypatch.setattr(nh, "_batch_v1", k8s.batch, raising=False)
    k8s.batch.delete_namespaced_job = boom_delete
    nh._cleanup_finished_jobs(NOW)


def test_cleanup_respects_success_retention(monkeypatch, k8s):
    monkeypatch.setattr(nh, "JOB_SUCCESS_RETENTION_S", 60.0)
    k8s.batch.jobs = [fake_job(name="fresh", succeeded=1, finished_ago=5)]
    nh._cleanup_finished_jobs(NOW)
    assert k8s.batch.deleted == []


def test_update_metrics_fallback_empty_nodes(metrics_env, monkeypatch):
    monkeypatch.setattr(nh, "_list_target_nodes", lambda: [])
    metrics_env(
        "/depot/",
        "",
        ok=True,
        timestamp=time.time(),
        ping_ms=1.0,
    )
    nh.update_metrics()
    assert sample("af_node_mount_valid", node="unknown") == 1


def test_timeout_result_without_partial_ping(metrics_env):
    metrics_env("/depot/", "node-a", ok=True, timeout=True, timestamp=time.time())
    nh.update_metrics()
    assert sample("af_node_mount_ping_ms") == nh._timeout_ping_ms()
