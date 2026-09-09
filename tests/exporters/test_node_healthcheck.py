"""Tests for docker/af-node-monitor/node_healthcheck.py.

The kubernetes client is faked at module-global level (the real package is
not installed in this suite), so the discovery logic — node and probe-pod
listing, the metric decision matrix — is tested without a cluster.

The exporter no longer creates anything: probes run as DaemonSets
(apps/monitoring/af-monitoring/daemonset-af-node-probe.yaml). What is tested
here is that it never confuses the three states those probes can be in — mount
broken, probe broken, results unreadable.
"""

import json
import time
import types

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


def fake_pod(mount="depot", node="node-a", ready=True, phase="Running", deleting=False):
    conds = [types.SimpleNamespace(type="Ready", status="True" if ready else "False")]
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=f"af-node-probe-{mount}-xyz",
            labels={"app": "af-node-monitor", "component": "probe", "mount": mount},
            deletion_timestamp=NOW if deleting else None,
        ),
        spec=types.SimpleNamespace(node_name=node),
        status=types.SimpleNamespace(phase=phase, conditions=conds),
    )


def fake_job(name="job-1"):
    return types.SimpleNamespace(metadata=types.SimpleNamespace(name=name))


class FakeCoreV1:
    def __init__(self, nodes, pods=None):
        self.nodes = nodes
        self.pods = pods or []
        self.calls = 0

    def list_node(self, label_selector):
        self.calls += 1
        return types.SimpleNamespace(items=self.nodes)

    def list_namespaced_pod(self, namespace, label_selector=None):
        return types.SimpleNamespace(items=self.pods)


class FakeBatchV1:
    """Only what the one-shot legacy-Job sweep needs."""

    def __init__(self, jobs=None):
        self.jobs = jobs or []
        self.deleted = []

    def list_namespaced_job(self, namespace, label_selector=None):
        return types.SimpleNamespace(items=self.jobs)

    def delete_namespaced_job(self, name, namespace, propagation_policy, body=None):
        self.deleted.append(name)


@pytest.fixture(autouse=True)
def clear_metrics():
    for metric in (
        nh.mount_valid,
        nh.mount_ping_ms,
        nh.mount_data_rate_gbps,
        nh.mount_metadata_latency_ms,
        nh.mount_result_fresh,
        nh.mount_timeout_total,
        nh.mount_last_success_ts,
        nh.mount_probe_up,
    ):
        metric.clear()
    nh.monitor_results_available.set(1)
    yield


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
    monkeypatch.setattr(nh, "_af_nodes_cache", [])
    monkeypatch.setattr(nh, "_last_node_refresh", 0.0)
    nh._node_pools.clear()
    return types.SimpleNamespace(core=core, batch=batch)


def sample(name, mount="/depot/", node="node-a", node_pool="prod", **extra):
    labels = {
        "mount_name": mount,
        "mount_path": nh.MOUNTS[mount],
        "node": node,
        "node_pool": node_pool,
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


# ── node discovery ────────────────────────────────────────────────────────────


def test_list_af_nodes_includes_not_ready(k8s):
    """Probes cannot report from NotReady nodes, but metrics must still see
    them — otherwise last-known-good gauges freeze green while checks cannot
    run."""
    k8s.core.nodes = [fake_node("ready-1"), fake_node("broken", ready=False)]
    assert nh._list_af_nodes() == [
        ("broken", "prod", False),
        ("ready-1", "prod", True),
    ]


def test_list_af_nodes_is_cached(k8s):
    nh._list_af_nodes()
    first_calls = k8s.core.calls
    nh._list_af_nodes()
    assert k8s.core.calls == first_calls  # served from cache


def test_list_af_nodes_without_k8s(monkeypatch):
    monkeypatch.setattr(nh, "_k8s_ready", False)
    monkeypatch.setattr(nh, "_init_k8s", lambda: None)
    assert nh._list_af_nodes() == []


def _seed_pool_gauges(node: str, pool: str, *, latency: float = 10000.0) -> None:
    """Create the full set of status gauges for every mount under one pool."""
    for m_name, m_path in nh.MOUNTS.items():
        labels = {
            "mount_name": m_name,
            "mount_path": m_path,
            "node": node,
            "node_pool": pool,
        }
        nh.mount_valid.labels(**labels).set(0)
        nh.mount_ping_ms.labels(**labels).set(nh._timeout_ping_ms())
        nh.mount_metadata_latency_ms.labels(**labels).set(latency)
        nh.mount_data_rate_gbps.labels(**labels).set(0.0)
        nh.mount_result_fresh.labels(**labels).set(0)


def test_refresh_clears_inactive_pool_gauges(k8s):
    """Ghost node_pool series (e.g. frozen 10s timeout under prod while the
    node is only labelled cms-af-dev) must be removed on refresh — otherwise
    heatmap queries that join without node_pool sum them into a false red."""
    _seed_pool_gauges("node-a", "dev", latency=10000.0)
    _seed_pool_gauges("node-a", "prod", latency=10000.0)
    assert sample("af_node_mount_metadata_latency_ms", node_pool="dev") == 10000.0
    assert sample("af_node_mount_metadata_latency_ms", node_pool="prod") == 10000.0

    # FakeCoreV1 returns the node for both selectors → first match = prod.
    nh._last_node_refresh = 0.0
    nh._af_nodes_cache = []
    nh._refresh_node_caches()

    assert nh._node_pools["node-a"] == "prod"
    # Inactive pool wiped; active pool left alone (update_metrics republishes).
    assert sample("af_node_mount_metadata_latency_ms", node_pool="dev") is None
    assert sample("af_node_mount_valid", node_pool="dev") is None
    assert sample("af_node_mount_result_fresh", node_pool="dev") is None
    assert sample("af_node_mount_metadata_latency_ms", node_pool="prod") == 10000.0


def test_refresh_clears_gauges_for_departed_nodes(k8s):
    _seed_pool_gauges("node-gone", "dev", latency=10000.0)
    nh._node_pools["node-gone"] = "dev"
    k8s.core.nodes = [fake_node("node-a")]
    nh._last_node_refresh = 0.0
    nh._af_nodes_cache = []
    nh._refresh_node_caches()

    assert "node-gone" not in nh._node_pools
    assert (
        sample("af_node_mount_metadata_latency_ms", node="node-gone", node_pool="dev")
        is None
    )


def test_pool_flip_dev_to_prod_drops_dev_series(k8s):
    """After cms-af-prod is added, the previous dev series must disappear."""
    _seed_pool_gauges("node-a", "dev", latency=42.0)
    nh._node_pools["node-a"] = "dev"
    # Healthy live value under the old pool.
    assert sample("af_node_mount_metadata_latency_ms", node_pool="dev") == 42.0

    nh._last_node_refresh = 0.0
    nh._af_nodes_cache = []
    nh._refresh_node_caches()

    assert nh._node_pools["node-a"] == "prod"
    assert sample("af_node_mount_metadata_latency_ms", node_pool="dev") is None


# ── update_metrics decision matrix ────────────────────────────────────────────


@pytest.fixture
def metrics_env(monkeypatch, tmp_path):
    """update_metrics with discovery stubbed and results in tmp_path."""
    monkeypatch.setattr(nh, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(nh, "_list_af_nodes", lambda: [("node-a", "prod", True)])
    monkeypatch.setattr(nh, "_probe_pod_states", lambda: {})
    nh._node_pools["node-a"] = "prod"

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
    assert sample("af_node_mount_result_fresh") == 1
    assert sample("af_node_mount_ping_ms") == 1.5
    assert sample("af_node_mount_metadata_latency_ms") == 20.0
    assert sample("af_node_mount_data_rate_gbps") == 8.2
    assert sample("af_node_mount_last_success_timestamp_seconds") > 0


def test_missing_result_reports_timeout_semantics(metrics_env):
    nh.update_metrics()

    assert sample("af_node_mount_valid") == 0
    assert sample("af_node_mount_result_fresh") == 0
    assert sample("af_node_mount_ping_ms") == nh._timeout_ping_ms()
    assert sample("af_node_mount_timeout_total", check_type="no_recent_result") == 1


def test_missing_result_with_a_probe_pod_is_never_started(metrics_env, monkeypatch):
    monkeypatch.setattr(nh, "_probe_pod_states", lambda: {("depot", "node-a"): False})

    nh.update_metrics()

    assert sample("af_node_mount_timeout_total", check_type="job_never_started") == 1


def test_stale_success_is_unknown(metrics_env, monkeypatch):
    """An old green result must not stay green — flip to unknown (fresh=0)."""
    monkeypatch.setattr(nh, "RESULT_STALE_WINDOW_S", 100.0)
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time() - 1000)

    nh.update_metrics()

    assert sample("af_node_mount_valid") == 0
    assert sample("af_node_mount_result_fresh") == 0
    assert sample("af_node_mount_timeout_total", check_type="stale_result") == 1


def test_stale_failure_stays_red(metrics_env, monkeypatch):
    """EOS (and similar) timeouts that stop refreshing must keep fresh=1 so
    AFMountInvalid keeps firing — not silently become unknown."""
    monkeypatch.setattr(nh, "RESULT_STALE_WINDOW_S", 100.0)
    metrics_env(
        "/depot/",
        "node-a",
        ok=False,
        timeout=True,
        timestamp=time.time() - 1000,
        ping_ms=10000.0,
    )

    nh.update_metrics()

    assert sample("af_node_mount_valid") == 0
    assert sample("af_node_mount_result_fresh") == 1
    assert sample("af_node_mount_timeout_total", check_type="job_result") == 1


def test_not_ready_node_clears_gauges_to_null(metrics_env, monkeypatch):
    """Power outage / NotReady: Jobs cannot start. Drop gauges so Prometheus
    scrapes null — not last-known-good green, and not a false red that would
    fire AFMountInvalid (that requires fresh=1 after a completed check)."""
    metrics_env(
        "/depot/",
        "node-a",
        ok=True,
        timestamp=time.time(),
        ping_ms=1.0,
        metadata_ms=10.0,
        throughput_gbps=5.0,
    )
    # First publish the green result while Ready…
    nh.update_metrics()
    assert sample("af_node_mount_valid") == 1
    assert sample("af_node_mount_result_fresh") == 1

    # …then the node goes NotReady: series must disappear.
    monkeypatch.setattr(nh, "_list_af_nodes", lambda: [("node-a", "prod", False)])
    nh.update_metrics()

    assert sample("af_node_mount_valid") is None
    assert sample("af_node_mount_result_fresh") is None
    assert sample("af_node_mount_metadata_latency_ms") is None
    assert sample("af_node_mount_ping_ms") is None
    assert sample("af_node_mount_data_rate_gbps") is None
    assert sample("af_node_mount_last_success_timestamp_seconds") is None


def test_completed_failure_on_ready_node_is_fresh_invalid(metrics_env):
    """A finished check that failed is red: valid=0 with fresh=1 — the only
    combination AFMountInvalid matches."""
    metrics_env(
        "/depot/",
        "node-a",
        ok=False,
        timestamp=time.time(),
        ping_ms=5.0,
        metadata_ms=12.0,
    )
    nh.update_metrics()
    assert sample("af_node_mount_valid") == 0
    assert sample("af_node_mount_result_fresh") == 1


def test_timeout_result_uses_worst_case_latencies(metrics_env):
    metrics_env(
        "/depot/", "node-a", ok=True, timeout=True, timestamp=time.time(), ping_ms=2.0
    )

    nh.update_metrics()

    assert sample("af_node_mount_valid") == 0  # timeout invalidates ok
    assert sample("af_node_mount_result_fresh") == 1  # completed check → red, not null
    assert sample("af_node_mount_ping_ms") == 2.0  # partial measurement kept
    assert sample("af_node_mount_metadata_latency_ms") == nh._timeout_metadata_ms()
    assert sample("af_node_mount_data_rate_gbps") == 0.0
    assert sample("af_node_mount_timeout_total", check_type="job_result") == 1


def test_storage_error_clears_gauges_to_null(metrics_env):
    # Seed a green series, then a storage error must wipe it (not leave green).
    metrics_env(
        "/depot/",
        "node-a",
        ok=True,
        timestamp=time.time(),
        ping_ms=1.0,
    )
    nh.update_metrics()
    assert sample("af_node_mount_valid") == 1

    nh._result_path("/depot/", "node-a").unlink()
    nh._result_path("/depot/", "node-a").mkdir()  # triggers _storage_error
    nh.update_metrics()

    assert sample("af_node_mount_valid") is None
    assert sample("af_node_mount_result_fresh") is None


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


def test_update_metrics_fallback_empty_nodes(metrics_env, monkeypatch):
    monkeypatch.setattr(nh, "_list_af_nodes", lambda: [])
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


# ── probe discovery ───────────────────────────────────────────────────────────


def test_probe_pod_states_keys_on_spec_node_name(k8s):
    """One DaemonSet template covers every node, so the node cannot come from
    a pod label the way it did from the per-node Jobs."""
    k8s.core.pods = [
        fake_pod(mount="depot", node="node-a", ready=True),
        fake_pod(mount="work", node="node-b", ready=False),
    ]
    assert nh._probe_pod_states() == {
        ("depot", "node-a"): True,
        ("work", "node-b"): False,
    }


def test_probe_pod_not_running_is_not_ready(k8s):
    """Pending or ContainerCreating — a volume that will not mount looks
    exactly like this, and it must not read as coverage."""
    k8s.core.pods = [fake_pod(phase="Pending", ready=False)]
    assert nh._probe_pod_states() == {("depot", "node-a"): False}


def test_terminating_probe_pod_is_not_ready(k8s):
    k8s.core.pods = [fake_pod(ready=True, deleting=True)]
    assert nh._probe_pod_states() == {("depot", "node-a"): False}


def test_probe_pod_rollout_overlap_takes_ready(k8s):
    """Both pods exist for a moment during a rollout; the Ready one wins."""
    k8s.core.pods = [
        fake_pod(ready=False, deleting=True),
        fake_pod(ready=True),
    ]
    assert nh._probe_pod_states() == {("depot", "node-a"): True}


def test_probe_pod_states_ignores_unplaced_and_unlabelled(k8s):
    k8s.core.pods = [
        fake_pod(node=""),  # not scheduled yet
        types.SimpleNamespace(
            metadata=types.SimpleNamespace(labels={}, deletion_timestamp=None),
            spec=types.SimpleNamespace(node_name="node-a"),
            status=types.SimpleNamespace(phase="Running", conditions=[]),
        ),
    ]
    assert nh._probe_pod_states() == {}


def test_probe_pod_states_is_none_when_the_api_cannot_answer(monkeypatch, k8s):
    """None, not {} — an empty map would say "no probe on any node", which is
    a fleet-wide monitoring outage reported over one refused list call."""
    monkeypatch.setattr(nh, "_k8s_ready", False)
    assert nh._probe_pod_states() is None

    monkeypatch.setattr(nh, "_k8s_ready", True)

    def boom(**kwargs):
        raise nh.ApiException("denied")

    k8s.core.list_namespaced_pod = boom
    assert nh._probe_pod_states() is None


def test_probe_up_is_absent_when_the_pod_list_is_unavailable(metrics_env, monkeypatch):
    monkeypatch.setattr(nh, "_probe_pod_states", lambda: {("depot", "node-a"): True})
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") == 1

    monkeypatch.setattr(nh, "_probe_pod_states", lambda: None)
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") is None
    # The mount verdict is unaffected: it comes from the result file.
    assert sample("af_node_mount_valid") == 1


def test_probe_pod_without_ready_condition_is_not_ready(k8s):
    pod = fake_pod()
    pod.status.conditions = []
    k8s.core.pods = [pod]
    assert nh._probe_pod_states() == {("depot", "node-a"): False}


# ── legacy Job sweep ──────────────────────────────────────────────────────────


def test_cleanup_legacy_jobs_deletes_everything_it_finds(k8s):
    k8s.batch.jobs = [fake_job("af-node-monitor-depot-a337-1"), fake_job("x")]
    nh._cleanup_legacy_jobs()
    assert k8s.batch.deleted == ["af-node-monitor-depot-a337-1", "x"]


def test_cleanup_legacy_jobs_survives_api_errors(monkeypatch, k8s):
    monkeypatch.setattr(nh, "_k8s_ready", False)
    nh._cleanup_legacy_jobs()
    assert k8s.batch.deleted == []

    monkeypatch.setattr(nh, "_k8s_ready", True)

    def boom(**kwargs):
        raise nh.ApiException("denied")

    k8s.batch.list_namespaced_job = boom
    nh._cleanup_legacy_jobs()

    k8s.batch = FakeBatchV1(jobs=[fake_job("stuck")])
    monkeypatch.setattr(nh, "_batch_v1", k8s.batch, raising=False)
    k8s.batch.delete_namespaced_job = boom
    nh._cleanup_legacy_jobs()  # does not raise


# ── probe_up: broken mount vs broken probe ────────────────────────────────────


def test_probe_up_reports_a_ready_probe(metrics_env, monkeypatch):
    monkeypatch.setattr(nh, "_probe_pod_states", lambda: {("depot", "node-a"): True})
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") == 1
    assert sample("af_node_mount_valid") == 1


def test_probe_up_zero_when_no_probe_pod_exists(metrics_env):
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") == 0
    # The mount verdict still stands on the last result it did produce.
    assert sample("af_node_mount_valid") == 1


def test_probe_up_cleared_for_not_ready_nodes(metrics_env, monkeypatch):
    """NotReady nodes publish nothing at all, probe state included."""
    monkeypatch.setattr(nh, "_probe_pod_states", lambda: {("depot", "node-a"): True})
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") == 1

    monkeypatch.setattr(nh, "_list_af_nodes", lambda: [("node-a", "prod", False)])
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") is None


# ── clock skew ────────────────────────────────────────────────────────────────


def test_future_timestamp_is_not_fresh(metrics_env):
    """A node whose clock runs ahead can never go stale, so a dead mount there
    would sit green forever."""
    metrics_env(
        "/depot/",
        "node-a",
        ok=True,
        timestamp=time.time() + 10 * nh.CLOCK_SKEW_TOLERANCE_S,
        ping_ms=1.0,
    )
    nh.update_metrics()
    assert sample("af_node_mount_result_fresh") == 0
    assert sample("af_node_mount_timeout_total", check_type="bad_timestamp") == 1


def test_missing_timestamp_is_not_fresh(metrics_env):
    metrics_env("/depot/", "node-a", ok=True, ping_ms=1.0)
    nh.update_metrics()
    assert sample("af_node_mount_result_fresh") == 0
    assert sample("af_node_mount_timeout_total", check_type="bad_timestamp") == 1


def test_unparseable_timestamp_is_not_fresh(metrics_env):
    metrics_env("/depot/", "node-a", ok=True, timestamp="soon", ping_ms=1.0)
    nh.update_metrics()
    assert sample("af_node_mount_result_fresh") == 0


def test_small_skew_is_still_accepted(metrics_env):
    metrics_env(
        "/depot/",
        "node-a",
        ok=True,
        timestamp=time.time() + nh.CLOCK_SKEW_TOLERANCE_S / 2,
        ping_ms=1.0,
    )
    nh.update_metrics()
    assert sample("af_node_mount_valid") == 1


# ── unreadable results volume ─────────────────────────────────────────────────


def test_read_timeout_is_reported_as_unavailable_not_as_a_failing_mount(
    metrics_env, monkeypatch
):
    """A wedged CephFS read must not be published as a broken mount, and must
    not be silent either: the series go null and results_available goes 0."""
    monkeypatch.setattr(nh, "RESULTS_READ_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        nh, "_read_result_file", lambda path: time.sleep(5) or {"ok": True}
    )
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()

    assert sample("af_node_mount_valid") is None
    assert sample("af_node_mount_result_fresh") is None
    assert REGISTRY.get_sample_value("af_node_monitor_results_available") == 0


def test_one_wedged_read_stops_the_iteration_reading_more(metrics_env, monkeypatch):
    """Every further read would hang the same way; each one costs a leaked
    thread and 15s of the loop."""
    monkeypatch.setattr(nh, "RESULTS_READ_TIMEOUT_S", 0.05)
    reads = []

    def slow(path):
        reads.append(path)
        time.sleep(5)

    monkeypatch.setattr(nh, "_read_result_file", slow)
    nh.update_metrics()
    assert len(reads) == 1


def test_probe_state_survives_an_unreadable_results_volume(metrics_env, monkeypatch):
    """The one gauge that says whether monitoring itself is alive must not be
    cleared by the fault it is there to explain."""
    monkeypatch.setattr(nh, "RESULTS_READ_TIMEOUT_S", 0.05)
    monkeypatch.setattr(nh, "_read_result_file", lambda path: time.sleep(5))
    monkeypatch.setattr(
        nh,
        "_probe_pod_states",
        lambda: {(m, "node-a"): True for m in ("depot", "work", "eos", "cvmfs")},
    )
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") == 1
    assert sample("af_node_mount_valid") is None


def test_storage_error_sets_results_unavailable(metrics_env, monkeypatch):
    monkeypatch.setattr(
        nh,
        "_read_result_file",
        lambda path: (_ for _ in ()).throw(OSError("stale file handle")),
    )
    nh.update_metrics()
    assert REGISTRY.get_sample_value("af_node_monitor_results_available") == 0


def test_healthy_iteration_reports_results_available(metrics_env):
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()
    assert REGISTRY.get_sample_value("af_node_monitor_results_available") == 1
