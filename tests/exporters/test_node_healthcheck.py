"""Tests for docker/af-node-monitor/node_healthcheck.py.

The API clients are faked at module-global level, so the discovery logic —
node and probe-pod listing, the metric decision matrix — is tested without a
cluster.

The exporter must never confuse the three states of the probe DaemonSets:
mount broken, probe broken, probes unreachable.
"""

import datetime
import http.server
import json
import threading
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


def fake_container_status(
    running=True, waiting=None, last_terminated=None, restart_count=0
):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(
            running=types.SimpleNamespace() if running else None,
            waiting=types.SimpleNamespace(reason=waiting) if waiting else None,
        ),
        last_state=types.SimpleNamespace(
            terminated=(
                types.SimpleNamespace(reason=last_terminated)
                if last_terminated
                else None
            )
        ),
        restart_count=restart_count,
    )


def fake_pod(
    mount="depot",
    node="node-a",
    ready=True,
    phase="Running",
    deleting=False,
    ip="10.0.0.1",
    age_s=3600.0,
    conditions=None,
    containers=None,
    port=8080,
):
    conds = [types.SimpleNamespace(type="Ready", status="True" if ready else "False")]
    for ctype, cstatus in (conditions or {}).items():
        conds.append(types.SimpleNamespace(type=ctype, status=cstatus))
    ports = [types.SimpleNamespace(name="http", container_port=port)] if port else []
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=f"af-node-probe-{mount}-xyz",
            labels={"app": "af-node-monitor", "component": "probe", "mount": mount},
            deletion_timestamp=NOW if deleting else None,
            creation_timestamp=datetime.datetime.fromtimestamp(
                time.time() - age_s, tz=datetime.timezone.utc
            ),
        ),
        spec=types.SimpleNamespace(
            node_name=node, containers=[types.SimpleNamespace(ports=ports)]
        ),
        status=types.SimpleNamespace(
            phase=phase,
            conditions=conds,
            pod_ip=ip if phase == "Running" else None,
            container_statuses=containers
            if containers is not None
            else [fake_container_status(running=phase == "Running")],
        ),
    )


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
    nh._result_cache.clear()
    yield


@pytest.fixture
def k8s(monkeypatch):
    """Wire fake k8s clients into the module and reset its mutable state."""
    core = FakeCoreV1(nodes=[fake_node("node-a")])
    monkeypatch.setattr(nh, "_init_k8s", lambda: None)
    monkeypatch.setattr(nh, "_k8s_ready", True)
    # _core_v1 is an annotation-only declaration until _init_k8s runs
    monkeypatch.setattr(nh, "_core_v1", core, raising=False)
    monkeypatch.setattr(nh, "_af_nodes_cache", [])
    monkeypatch.setattr(nh, "_last_node_refresh", 0.0)
    monkeypatch.setattr(nh, "_probe_cache", None)
    monkeypatch.setattr(nh, "_probe_cache_ts", 0.0)
    nh._node_pools.clear()
    return types.SimpleNamespace(core=core)


def sample(name, mount="/depot/", node="node-a", node_pool="prod", **extra):
    labels = {
        "mount_name": mount,
        "mount_path": nh.MOUNTS[mount],
        "node": node,
        "node_pool": node_pool,
        **extra,
    }
    return REGISTRY.get_sample_value(name, labels)


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


def test_refresh_skips_nodes_without_a_name(k8s):
    unnamed = fake_node("x")
    unnamed.metadata.name = None
    k8s.core.nodes = [unnamed, fake_node("node-a")]
    assert nh._list_af_nodes() == [("node-a", "prod", True)]


def test_refresh_keeps_the_last_node_list_when_the_api_fails(k8s):
    """A refused list must not empty the node set: every mount on every node
    would stop reporting."""
    nh._list_af_nodes()
    nh._last_node_refresh = 0.0  # force a refresh

    def boom(label_selector):
        raise nh.ApiException("denied")

    k8s.core.list_node = boom
    assert nh._list_af_nodes() == [("node-a", "prod", True)]
    assert nh._node_pools == {"node-a": "prod"}


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


class Probes:
    """What update_metrics sees: probe pods on the node and what each serves."""

    def __init__(self):
        self.pods = {}
        self.served = {}
        self.listed = True

    def __call__(self, mount, node, /, **result):
        """A Ready probe for (mount, node) serving `result`."""
        key = (nh._sanitized_mount_name(mount), node)
        self.pods.setdefault(key, nh.Probe(ready=True, ip="10.0.0.1"))
        self.served[key] = result

    def pod(self, mount, node="node-a", **probe):
        self.pods[(nh._sanitized_mount_name(mount), node)] = nh.Probe(**probe)


@pytest.fixture
def metrics_env(monkeypatch):
    """update_metrics with discovery and the HTTP pulls stubbed."""
    probes = Probes()
    monkeypatch.setattr(nh, "_list_af_nodes", lambda: [("node-a", "prod", True)])
    monkeypatch.setattr(
        nh, "_probe_pods", lambda now: probes.pods if probes.listed else None
    )
    monkeypatch.setattr(
        nh,
        "_fetch_all",
        lambda targets: {k: v for k, v in probes.served.items() if k in targets},
    )
    nh._node_pools["node-a"] = "prod"
    return probes


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


def test_missing_result_with_a_probe_pod_is_never_started(metrics_env):
    metrics_env.pod("/depot/", ready=False)

    nh.update_metrics()

    assert sample("af_node_mount_timeout_total", check_type="job_never_started") == 1


def test_ready_probe_that_does_not_answer_is_unreachable(metrics_env):
    """Ready by the kubelet's own check but silent to the exporter: the path
    between them is broken, and probe_up must say so rather than blame the
    mount."""
    metrics_env.pod("/depot/", ready=True, ip="10.0.0.9")

    nh.update_metrics()

    assert sample("af_node_mount_result_fresh") == 0
    assert sample("af_node_mount_probe_up") == 0
    assert sample("af_node_mount_timeout_total", check_type="unreachable") == 1


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
    """A NotReady node's gauges are dropped, so Prometheus scrapes null — not
    last-known-good green, and not a red that fires AFMountInvalid."""
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


def test_an_unmeasured_rate_is_absent_not_zero(metrics_env):
    """A new probe reports no rate until its first fio run; the 0.0 an unknown
    pass wrote before it must not read as a measured zero on a healthy mount."""
    metrics_env.pod("/depot/", ready=True, ip="10.0.0.1")
    nh.update_metrics()
    assert sample("af_node_mount_data_rate_gbps") == 0.0

    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()

    assert sample("af_node_mount_valid") == 1
    assert sample("af_node_mount_data_rate_gbps") is None


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


def test_failure_without_latencies_publishes_sentinels(metrics_env):
    """A check that failed before it could time anything must not leave a
    previous healthy latency on the chart."""
    metrics_env("/depot/", "node-a", ok=False, timestamp=time.time())
    nh.update_metrics()
    assert sample("af_node_mount_valid") == 0
    assert sample("af_node_mount_ping_ms") == nh._timeout_ping_ms()
    assert sample("af_node_mount_metadata_latency_ms") == nh._timeout_metadata_ms()
    assert sample("af_node_mount_data_rate_gbps") == 0.0


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


def test_a_missed_pull_keeps_the_last_verdict(metrics_env):
    """One pull that fails must not turn a failing mount unknown: the cached
    verdict stands under the same staleness rules, and probe_up says the probe
    did not answer."""
    metrics_env("/depot/", "node-a", ok=False, timeout=True, timestamp=time.time())
    nh.update_metrics()
    assert sample("af_node_mount_valid") == 0
    assert sample("af_node_mount_result_fresh") == 1

    metrics_env.served.clear()
    nh.update_metrics()

    assert sample("af_node_mount_valid") == 0
    assert sample("af_node_mount_result_fresh") == 1
    assert sample("af_node_mount_probe_up") == 0


def test_a_cached_success_goes_unknown_once_stale(metrics_env, monkeypatch):
    monkeypatch.setattr(nh, "RESULT_STALE_WINDOW_S", 100.0)
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time() - 1000)
    nh.update_metrics()
    metrics_env.served.clear()
    nh.update_metrics()
    assert sample("af_node_mount_result_fresh") == 0


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

    class FakeConfig:
        @staticmethod
        def load_incluster_config():
            raise Exception("not in cluster")

        @staticmethod
        def load_kube_config():
            return None

    class FakeCore:
        pass

    class FakeClient:
        CoreV1Api = FakeCore

    monkeypatch.setattr(nh, "config", FakeConfig)
    monkeypatch.setattr(nh, "client", FakeClient)
    monkeypatch.setenv("AF_NODE_MONITOR_VERBOSE", "1")

    nh._init_k8s()
    assert nh._k8s_ready is True
    assert isinstance(nh._core_v1, FakeCore)

    # second call is a no-op once ready
    nh._init_k8s()


def test_update_metrics_without_nodes_publishes_no_mount_series(
    metrics_env, monkeypatch
):
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    monkeypatch.setattr(nh, "_list_af_nodes", lambda: [])
    nh.update_metrics()
    assert sample("af_node_mount_valid") is None


def test_timeout_result_without_partial_ping(metrics_env):
    metrics_env("/depot/", "node-a", ok=True, timeout=True, timestamp=time.time())
    nh.update_metrics()
    assert sample("af_node_mount_ping_ms") == nh._timeout_ping_ms()


# ── probe discovery ───────────────────────────────────────────────────────────


def test_probe_pods_key_on_spec_node_name(k8s):
    """One DaemonSet template covers every node, so the node cannot come from
    a pod label."""
    k8s.core.pods = [
        fake_pod(mount="depot", node="node-a", ready=True, ip="10.0.0.1"),
        fake_pod(mount="work", node="node-b", ready=False, ip="10.0.0.2"),
    ]
    probes = nh._list_probe_pods(time.time())
    assert probes[("depot", "node-a")] == nh.Probe(ready=True, ip="10.0.0.1")
    assert probes[("work", "node-b")] == nh.Probe(ready=False, ip="10.0.0.2")


def test_probe_pod_not_running_is_not_ready(k8s):
    """Pending or ContainerCreating — a volume that will not mount looks
    exactly like this, and it must not read as coverage."""
    k8s.core.pods = [fake_pod(phase="Pending", ready=False)]
    probe = nh._list_probe_pods(time.time())[("depot", "node-a")]
    assert (probe.ready, probe.ip) == (False, "")


def test_terminating_probe_pod_is_not_ready_or_asked(k8s):
    k8s.core.pods = [fake_pod(ready=True, deleting=True)]
    probe = nh._list_probe_pods(time.time())[("depot", "node-a")]
    assert (probe.ready, probe.ip) == (False, "")


def test_probe_pod_rollout_overlap_takes_ready(k8s):
    """Both pods exist for a moment during a rollout; the Ready one wins and is
    the one asked."""
    k8s.core.pods = [
        fake_pod(ready=False, ip="10.0.0.1"),
        fake_pod(ready=True, ip="10.0.0.2"),
    ]
    probe = nh._list_probe_pods(time.time())[("depot", "node-a")]
    assert (probe.ready, probe.ip) == (True, "10.0.0.2")


def test_probe_pods_ignore_unplaced_and_unlabelled(k8s):
    unlabelled = fake_pod()
    unlabelled.metadata.labels = {}
    k8s.core.pods = [fake_pod(node=""), unlabelled]
    assert nh._list_probe_pods(time.time()) == {}


def test_probe_port_comes_from_the_pod_spec(k8s):
    k8s.core.pods = [fake_pod(port=9090), fake_pod(mount="work", port=None)]
    probes = nh._list_probe_pods(time.time())
    assert probes[("depot", "node-a")].port == 9090
    assert probes[("work", "node-a")].port == nh.DEFAULT_PROBE_PORT


def test_probe_pod_without_ready_condition_is_not_ready(k8s):
    pod = fake_pod()
    pod.status.conditions = []
    k8s.core.pods = [pod]
    assert nh._list_probe_pods(time.time())[("depot", "node-a")].ready is False


def test_probe_pods_are_none_when_the_api_cannot_answer(monkeypatch, k8s):
    """None, not {} — an empty map would say "no probe on any node", which is
    a fleet-wide monitoring outage reported over one refused list call."""
    monkeypatch.setattr(nh, "_k8s_ready", False)
    assert nh._list_probe_pods(time.time()) is None

    monkeypatch.setattr(nh, "_k8s_ready", True)

    def boom(**kwargs):
        raise nh.ApiException("denied")

    k8s.core.list_namespaced_pod = boom
    assert nh._list_probe_pods(time.time()) is None


def test_a_refused_pod_list_reuses_the_last_one_for_a_while(monkeypatch, k8s):
    """Pod IPs are what the exporter pulls from; one refused list call must not
    cut it off from every probe at once."""
    k8s.core.pods = [fake_pod()]
    now = time.time()
    first = nh._probe_pods(now)

    def boom(**kwargs):
        raise nh.ApiException("denied")

    k8s.core.list_namespaced_pod = boom
    assert nh._probe_pods(now + 10) == first
    assert nh._probe_pods(now + nh.POD_CACHE_TTL_S + 1) is None


# ── a probe pod that cannot mount its volume ──────────────────────────────────


def setup_failed(pod):
    return nh._mount_setup_failed(pod, time.time())


def test_waiting_on_a_volume_past_the_grace_is_a_mount_failure():
    pod = fake_pod(
        phase="Pending",
        ready=False,
        age_s=nh.MOUNT_SETUP_GRACE_S + 60,
        conditions={"PodReadyToStartContainers": "False"},
    )
    assert setup_failed(pod)


def test_a_slow_start_is_not_a_mount_failure():
    pod = fake_pod(
        phase="Pending",
        ready=False,
        age_s=30,
        conditions={"PodReadyToStartContainers": "False"},
    )
    assert not setup_failed(pod)


def test_pulling_an_image_is_not_a_mount_failure():
    """The sandbox exists once volumes are mounted; a pod stuck after that is
    waiting on something else."""
    pod = fake_pod(
        phase="Pending",
        ready=False,
        age_s=nh.MOUNT_SETUP_GRACE_S + 60,
        conditions={"PodReadyToStartContainers": "True"},
        containers=[fake_container_status(running=False, waiting="ImagePullBackOff")],
    )
    assert not setup_failed(pod)


def test_pending_without_the_sandbox_condition_is_not_classified():
    pod = fake_pod(phase="Pending", ready=False, age_s=nh.MOUNT_SETUP_GRACE_S + 60)
    assert not setup_failed(pod)


@pytest.mark.parametrize(
    "status",
    [
        fake_container_status(
            running=False, waiting="RunContainerError", restart_count=3
        ),
        fake_container_status(
            running=False,
            waiting="CrashLoopBackOff",
            last_terminated="ContainerCannotRun",
            restart_count=140,
        ),
    ],
)
def test_a_runtime_that_cannot_bind_the_volume_is_a_mount_failure(status):
    pod = fake_pod(ready=False, containers=[status])
    assert setup_failed(pod)


@pytest.mark.parametrize(
    "status",
    [
        fake_container_status(
            running=False, waiting="RunContainerError", restart_count=1
        ),
        fake_container_status(
            running=False,
            waiting="CrashLoopBackOff",
            last_terminated="Error",
            restart_count=9,
        ),
        fake_container_status(
            running=True, last_terminated="ContainerCannotRun", restart_count=9
        ),
    ],
)
def test_other_container_trouble_is_not_a_mount_failure(status):
    pod = fake_pod(ready=False, containers=[status])
    assert not setup_failed(pod)


def test_a_terminating_pod_is_not_a_mount_failure():
    pod = fake_pod(
        phase="Pending",
        ready=False,
        deleting=True,
        age_s=nh.MOUNT_SETUP_GRACE_S + 60,
        conditions={"PodReadyToStartContainers": "False"},
    )
    assert not setup_failed(pod)


def test_a_ready_replacement_clears_a_stuck_pod(k8s):
    stuck = fake_pod(
        phase="Pending",
        ready=False,
        age_s=nh.MOUNT_SETUP_GRACE_S + 60,
        conditions={"PodReadyToStartContainers": "False"},
    )
    k8s.core.pods = [stuck]
    assert nh._list_probe_pods(time.time())[("depot", "node-a")].setup_failed
    k8s.core.pods = [stuck, fake_pod(ready=True)]
    assert not nh._list_probe_pods(time.time())[("depot", "node-a")].setup_failed


def test_setup_failure_beside_a_working_sibling_is_red(metrics_env):
    """The sibling proves the node, network, image and scripts work, so what
    stops this pod is its volume — and a session landing here would hang the
    same way."""
    metrics_env("/work/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    metrics_env.pod("/depot/", ready=False, setup_failed=True)

    nh.update_metrics()

    assert sample("af_node_mount_valid") == 0
    assert sample("af_node_mount_result_fresh") == 1
    assert sample("af_node_mount_probe_up") == 0
    assert sample("af_node_mount_timeout_total", check_type="mount_setup_failed") == 1


def test_setup_failure_with_no_working_sibling_is_unknown(metrics_env):
    """Nothing on the node answers, so the node itself may be the fault."""
    metrics_env.pod("/depot/", ready=False, setup_failed=True)

    nh.update_metrics()

    assert sample("af_node_mount_result_fresh") == 0


def test_setup_failure_overrides_a_cached_success(metrics_env):
    """A verdict from the pod that ran before says nothing about the one that
    cannot start now."""
    metrics_env("/work/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()
    assert sample("af_node_mount_valid") == 1

    del metrics_env.served[("depot", "node-a")]
    metrics_env.pod("/depot/", ready=False, setup_failed=True)
    nh.update_metrics()

    assert sample("af_node_mount_valid") == 0
    assert sample("af_node_mount_result_fresh") == 1


# ── probe_up: broken mount vs broken probe ────────────────────────────────────


def test_probe_up_reports_a_ready_probe_that_answers(metrics_env):
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") == 1
    assert sample("af_node_mount_valid") == 1


def test_probe_up_zero_when_the_probe_pod_is_gone(metrics_env):
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()

    metrics_env.pods.clear()
    nh.update_metrics()

    assert sample("af_node_mount_probe_up") == 0
    # The mount verdict still stands on the last result it did produce.
    assert sample("af_node_mount_valid") == 1


def test_probe_up_is_absent_when_the_pod_list_is_unavailable(metrics_env):
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") == 1

    metrics_env.listed = False
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") is None
    assert sample("af_node_mount_valid") == 1


def test_probe_up_cleared_for_not_ready_nodes(metrics_env, monkeypatch):
    """NotReady nodes publish nothing at all, probe state included."""
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") == 1

    monkeypatch.setattr(nh, "_list_af_nodes", lambda: [("node-a", "prod", False)])
    nh.update_metrics()
    assert sample("af_node_mount_probe_up") is None


def test_probes_on_not_ready_nodes_are_not_asked(metrics_env, monkeypatch):
    asked = []
    monkeypatch.setattr(nh, "_fetch_all", lambda targets: asked.extend(targets) or {})
    monkeypatch.setattr(nh, "_list_af_nodes", lambda: [("node-a", "prod", False)])
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time())
    nh.update_metrics()
    assert asked == []


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


# ── the exporter's path to the probes ─────────────────────────────────────────


def results_available():
    return REGISTRY.get_sample_value("af_node_monitor_results_available")


def test_every_ready_probe_answering_is_full_availability(metrics_env):
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    metrics_env("/work/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    nh.update_metrics()
    assert results_available() == 1


def test_availability_is_the_fraction_of_ready_probes_answering(metrics_env):
    metrics_env("/depot/", "node-a", ok=True, timestamp=time.time(), ping_ms=1.0)
    metrics_env.pod("/work/", ready=True, ip="10.0.0.2")
    metrics_env.pod("/eos/", ready=False, ip="10.0.0.3")  # not counted
    nh.update_metrics()
    assert results_available() == 0.5


def test_no_ready_probe_on_a_ready_node_is_no_availability(metrics_env):
    metrics_env.pod("/depot/", ready=False)
    nh.update_metrics()
    assert results_available() == 0


def test_an_unlisted_fleet_is_no_availability(metrics_env):
    metrics_env.listed = False
    nh.update_metrics()
    assert results_available() == 0


def test_no_ready_node_leaves_nothing_to_miss(metrics_env, monkeypatch):
    monkeypatch.setattr(nh, "_list_af_nodes", lambda: [("node-a", "prod", False)])
    nh.update_metrics()
    assert results_available() == 1


# ── pulling results ───────────────────────────────────────────────────────────


@pytest.fixture
def http_probe():
    """A local HTTP server standing in for a probe pod."""
    responses = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code, body = responses.get(self.path, (404, b""))
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], responses
    server.shutdown()
    server.server_close()


def test_fetch_result_reads_the_probe_json(http_probe):
    port, responses = http_probe
    responses["/result"] = (200, json.dumps({"ok": True}).encode())
    assert nh._fetch_result("127.0.0.1", port) == {"ok": True}


@pytest.mark.parametrize(
    "response", [(404, b"no result yet"), (200, b"{ nope"), (200, b"[1, 2]")]
)
def test_fetch_result_without_a_usable_answer_is_none(http_probe, response):
    port, responses = http_probe
    responses["/result"] = response
    assert nh._fetch_result("127.0.0.1", port) is None


def test_fetch_result_from_nothing_listening_is_none(http_probe):
    port, _ = http_probe
    assert nh._fetch_result("127.0.0.1", 1) is None


def test_fetch_result_brackets_an_ipv6_address(monkeypatch):
    urls = []

    def fake_open(url, timeout):
        urls.append(url)
        raise OSError("unreachable")

    monkeypatch.setattr(nh._opener, "open", fake_open)
    nh._fetch_result("fd00::5", 8080)
    assert urls == ["http://[fd00::5]:8080/result"]


def test_one_slow_probe_cannot_stall_the_pass(monkeypatch):
    monkeypatch.setattr(nh, "FETCH_PASS_TIMEOUT_S", 0.2)

    def fetch(ip, port):
        if ip == "slow":
            time.sleep(2)
        return {"ip": ip}

    monkeypatch.setattr(nh, "_fetch_result", fetch)
    started = time.time()
    got = nh._fetch_all(
        {
            ("depot", "a"): nh.Probe(ready=True, ip="fast"),
            ("depot", "b"): nh.Probe(ready=True, ip="slow"),
        }
    )
    assert time.time() - started < 1.5
    assert got == {("depot", "a"): {"ip": "fast"}}


# ── gauge clearing ────────────────────────────────────────────────────────────


def test_clear_gauges_tolerates_a_never_published_label_set():
    """Older prometheus_client raises KeyError from remove() for an unknown
    label set; one of those must not stop the rest being cleared."""
    labels = {
        "mount_name": "/depot/",
        "mount_path": "/depot/",
        "node": "node-a",
        "node_pool": "prod",
    }
    nh.mount_valid.labels(**labels).set(1)

    class Strict:
        def remove(self, *labelvalues):
            raise KeyError(labelvalues)

    nh._clear_gauges(labels, (Strict(), nh.mount_valid))
    assert sample("af_node_mount_valid") is None


def test_clear_gauges_holds_objects_not_names():
    """Resolving a gauge by name through globals() turns a typo into the
    KeyError that _clear_gauges swallows, and a gauge that is silently never
    cleared is the frozen last-known-good green the clearing exists to stop."""
    assert all(hasattr(g, "remove") for g in nh.RESULT_GAUGES)
    assert nh.mount_probe_up in nh.ALL_MOUNT_GAUGES
    assert nh.mount_probe_up not in nh.RESULT_GAUGES
    assert set(nh.RESULT_GAUGES) < set(nh.ALL_MOUNT_GAUGES)
