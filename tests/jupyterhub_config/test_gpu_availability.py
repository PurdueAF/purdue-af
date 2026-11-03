"""Tests for extraFiles/gpu-availability.py — live GPU counts + spawn refusal."""

import copy
import logging
import types

import prometheus_client
import pytest
from common import ConfigSink, hub_values
from hub_helpers import load_snippet
from prometheus_client import CollectorRegistry

SLICE = "nvidia.com/mig-1g.5gb"
FULL = "nvidia.com/mig-7g.40gb"
T4 = "nvidia.com/gpu"
PROFILE_LIST = hub_values()["singleuser"]["profileList"]
HEDGE = " - subject to availability"
NOTE = "idle session timeout 24h"
ONE_FULL_FREE = {SLICE: 0, FULL: 1, T4: 0}


def culler_service():
    return {"name": "gpu-culler", **hub_values()["hub"]["services"]["gpu-culler"]}


def load(monkeypatch, profile_list=...):
    monkeypatch.setattr(prometheus_client, "REGISTRY", CollectorRegistry())
    sink = ConfigSink()
    sink.JupyterHub.services = [culler_service()]
    sink.KubeSpawner.profile_list = (
        copy.deepcopy(PROFILE_LIST) if profile_list is ... else profile_list
    )
    return load_snippet("gpu-availability.py", monkeypatch, extra_globals={"c": sink})


def load_with_services(monkeypatch, services):
    monkeypatch.setattr(prometheus_client, "REGISTRY", CollectorRegistry())
    sink = ConfigSink()
    sink.JupyterHub.services = services
    sink.KubeSpawner.profile_list = copy.deepcopy(PROFILE_LIST)
    return load_snippet("gpu-availability.py", monkeypatch, extra_globals={"c": sink})


def set_free(ns, value):
    """Stub the availability lookups the snippet's callables resolve at runtime.

    The form reads free_gpus (post-reservation); the spawn gate reads
    observed_free_gpus and subtracts reservations itself.
    """

    async def _free(use_cache=True):
        return value

    ns["free_gpus"] = _free
    ns["observed_free_gpus"] = _free


def stub_prometheus(ns, free):
    """Stub the query the caches and reservations sit on top of."""

    async def _free():
        return free

    ns["get_free_gpus"] = _free


def gpu_choices(profiles):
    return profiles[0]["profile_options"]["1-gpu"]["choices"]


def gpu_label(profiles, resource):
    """The form label of the choice that requests `resource`."""
    (label,) = [
        choice["display_name"]
        for choice in gpu_choices(profiles).values()
        if choice["kubespawner_override"]["extra_resource_limits"].get(resource)
    ]
    return label


def hedged(resource):
    return gpu_label(PROFILE_LIST, resource)


def plain(resource):
    return hedged(resource).removesuffix(HEDGE)


def fake_pod(limits):
    main = types.SimpleNamespace(resources=types.SimpleNamespace(limits=limits))
    # KubeSpawner leaves an extra container's resources a plain dict
    sidecar = types.SimpleNamespace(resources={"limits": {"memory": "256Mi"}})
    return types.SimpleNamespace(spec=types.SimpleNamespace(containers=[main, sidecar]))


def fake_spawner(user="someone"):
    return types.SimpleNamespace(
        log=logging.getLogger("test-spawner"),
        name="",  # the default (unnamed) server
        user=types.SimpleNamespace(name=user),
    )


def refusals(ns, resource, reason):
    """Current value of the refusal counter."""
    return (
        ns["REGISTRY"].get_sample_value(
            "purdue_af_hub_gpu_spawn_refused_total",
            {"resource": resource, "reason": reason},
        )
        or 0.0
    )


# ── profile form annotation ───────────────────────────────────────────────────


async def test_form_shows_live_counts(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 12, FULL: 2, T4: 5})

    profiles = await ns["profile_list_with_gpu_counts"](None)

    # live counts are joined with each flavor's idle-timeout note, and the
    # static hedge values.yaml carries is replaced by the live count
    assert hedged(FULL).endswith(HEDGE)
    assert gpu_label(profiles, SLICE) == f"{plain(SLICE)} — 12 available now, {NOTE}"
    assert gpu_label(profiles, FULL) == f"{plain(FULL)} — 2 available now, {NOTE}"
    assert gpu_label(profiles, T4) == f"{plain(T4)} — 5 available now, {NOTE}"
    # zero-GPU choice and non-GPU options stay untouched
    static = PROFILE_LIST[0]["profile_options"]
    assert gpu_choices(profiles)["1"] == static["1-gpu"]["choices"]["1"]
    assert profiles[0]["profile_options"]["0-cpu"] == static["0-cpu"]


async def test_form_marks_exhausted_flavor(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 3, FULL: 0, T4: 0})

    profiles = await ns["profile_list_with_gpu_counts"](None)

    exhausted = f"none available right now, {NOTE}"
    assert gpu_label(profiles, SLICE) == f"{plain(SLICE)} — 3 available now, {NOTE}"
    assert gpu_label(profiles, FULL) == f"{plain(FULL)} — {exhausted}"
    assert gpu_label(profiles, T4) == f"{plain(T4)} — {exhausted}"


async def test_form_keeps_static_labels_when_availability_unknown(monkeypatch):
    ns = load(monkeypatch)
    stub_prometheus(ns, None)

    profiles = await ns["profile_list_with_gpu_counts"](None)

    # no live counts: the static hedge stays, but the idle-timeout notes show
    for resource in (SLICE, FULL, T4):
        assert gpu_label(profiles, resource) == f"{hedged(resource)} — {NOTE}"


async def test_no_culler_means_no_note_and_keeps_the_label(monkeypatch):
    ns = load_with_services(monkeypatch, [])
    set_free(ns, None)

    profiles = await ns["profile_list_with_gpu_counts"](None)

    assert gpu_label(profiles, FULL) == hedged(FULL)


async def test_static_profile_list_is_not_mutated(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 1, FULL: 1, T4: 1})

    await ns["profile_list_with_gpu_counts"](None)

    assert ns["_static_profile_list"] == PROFILE_LIST


# ── availability lookup ───────────────────────────────────────────────────────


async def test_get_free_gpus_subtracts_usage(monkeypatch):
    ns = load(monkeypatch)

    async def prom(query):
        if "kube_node_status_allocatable" in query:
            return {
                "nvidia_com_mig_1g_5gb": 21.0,
                "nvidia_com_mig_7g_40gb": 2.0,
                "nvidia_com_gpu": 8.0,
            }
        return {
            "nvidia_com_mig_1g_5gb": 20.0,  # no 40GB pods running
            "nvidia_com_gpu": 3.0,
        }

    ns["_prom_query"] = prom

    assert await ns["get_free_gpus"]() == {SLICE: 1, FULL: 2, T4: 5}


async def test_get_free_gpus_clamps_at_zero(monkeypatch):
    ns = load(monkeypatch)

    async def prom(query):
        if "kube_node_status_allocatable" in query:
            return {"nvidia_com_mig_1g_5gb": 1.0}  # no 40GB/T4 capacity at all
        return {"nvidia_com_mig_1g_5gb": 5.0}  # over-reported usage

    ns["_prom_query"] = prom

    assert await ns["get_free_gpus"]() == {SLICE: 0, FULL: 0, T4: 0}


async def test_prom_query_maps_samples_by_resource(monkeypatch):
    ns = load(monkeypatch)
    ns["PROMETHEUS_URL"] = "http://prom:9090"
    fetched = []

    class FakeClient:
        async def fetch(self, url, **kwargs):
            fetched.append((url, kwargs))
            body = (
                b'{"data": {"result": ['
                b'{"metric": {"resource": "nvidia_com_gpu"}, "value": [0, "3"]}]}}'
            )
            return types.SimpleNamespace(body=body)

    ns["AsyncHTTPClient"] = FakeClient

    assert await ns["_prom_query"]("up") == {"nvidia_com_gpu": 3.0}
    url, kwargs = fetched[0]
    assert url == "http://prom:9090/api/v1/query?query=up"
    # a hung Prometheus must not hold the form or the spawn
    assert kwargs == {"connect_timeout": 5, "request_timeout": 5}


@pytest.mark.parametrize(
    "command, note",
    [
        (["python3", "--timeout=5400", "--every=600"], "idle session timeout 1.5h"),
        (["python3", "--timeout=soon"], None),
        (["python3"], None),
    ],
)
def test_note_follows_the_culler_command(monkeypatch, command, note):
    culler = {"name": "gpu-culler", "command": command}
    assert load_with_services(monkeypatch, [culler])["IDLE_NOTE"] == note


def test_note_reads_only_the_gpu_culler(monkeypatch):
    other = {"name": "prometheus", "command": ["run", "--timeout=60"]}
    assert load_with_services(monkeypatch, [other])["IDLE_NOTE"] is None


def test_every_gpu_on_the_form_has_a_flavor_and_a_metric(monkeypatch):
    ns = load(monkeypatch)
    offered = {
        resource
        for profile in PROFILE_LIST
        for option in profile.get("profile_options", {}).values()
        for choice in option["choices"].values()
        for resource in choice["kubespawner_override"].get("extra_resource_limits", {})
    }
    assert set(ns["GPU_FLAVORS"]) == set(ns["GPU_METRICS"])
    assert offered <= set(ns["GPU_METRICS"])


def test_snippet_finds_gpu_queries_in_its_config_dir(monkeypatch):
    """z2jh execs the snippet, which puts the mounted directory on sys.path."""
    import sys

    mounts = hub_values()["hub"]["extraVolumeMounts"]
    mount = next(m for m in mounts if m["name"] == "hub-extra-config")

    ns = load(monkeypatch)

    assert ns["_CONFIG_D"] == mount["mountPath"]
    assert sys.path[0] == mount["mountPath"]


async def test_get_free_gpus_unknown_on_query_failure(monkeypatch):
    ns = load(monkeypatch)
    warnings = []
    ns["log"] = types.SimpleNamespace(
        warning=lambda message, *args: warnings.append(message % args)
    )

    async def prom(query):
        raise OSError("connection refused")

    ns["_prom_query"] = prom

    assert await ns["get_free_gpus"]() is None
    assert warnings == [
        "[gpu-availability] Prometheus query failed: connection refused"
    ]


async def test_get_free_gpus_unknown_when_metrics_missing(monkeypatch):
    ns = load(monkeypatch)

    async def prom(query):
        return {}

    ns["_prom_query"] = prom

    assert await ns["get_free_gpus"]() is None


# ── spawn-time enforcement ────────────────────────────────────────────────────


async def test_spawn_refused_when_flavor_exhausted(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 5, FULL: 0, T4: 0})

    with pytest.raises(ns["GPUsUnavailableError"], match="full A100 GPUs"):
        await ns["refuse_gpu_spawn_if_unavailable"](
            fake_spawner(), fake_pod({FULL: "1"})
        )

    with pytest.raises(ns["GPUsUnavailableError"], match="NVIDIA T4 GPUs"):
        await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), fake_pod({T4: "1"}))


async def test_spawn_allowed_when_available(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 5, FULL: 1, T4: 2})
    pod = fake_pod({FULL: "1", "cpu": "256", "memory": "256G"})

    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod

    pod = fake_pod({T4: "1"})
    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod


async def test_non_gpu_spawn_skips_availability_check(monkeypatch):
    ns = load(monkeypatch)
    # calling either would raise TypeError
    ns["free_gpus"] = ns["observed_free_gpus"] = None

    pod = fake_pod({"cpu": "256", "memory": "256G"})
    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod

    # a zero-valued GPU limit (the "0 GPUs" choice) is not a GPU request
    pod = fake_pod({SLICE: 0})
    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod


async def test_non_gpu_spawn_hides_node_gpus(monkeypatch):
    # the NVIDIA-based 0.13.x image inherits NVIDIA_VISIBLE_DEVICES=all; the
    # hook must neutralize it for 0-GPU pods — in EVERY container (sidecars
    # must not see GPUs either)
    ns = load(monkeypatch)
    pod = fake_pod({"cpu": "256", "memory": "256G"})

    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod

    for container in pod.spec.containers:
        env = {e.name: e.value for e in container.env}
        assert env == {"NVIDIA_VISIBLE_DEVICES": "void"}


async def test_gpu_spawn_env_left_untouched(monkeypatch):
    # GPU sessions must keep exactly the device-plugin-injected environment:
    # the hook may not add or change NVIDIA_VISIBLE_DEVICES for them
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 5, FULL: 1, T4: 1})
    pod = fake_pod({FULL: "1"})

    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod

    for container in pod.spec.containers:
        assert not getattr(container, "env", None)


async def test_hide_gpus_respects_existing_env(monkeypatch):
    # an explicitly configured NVIDIA_VISIBLE_DEVICES is never overridden
    ns = load(monkeypatch)
    pod = fake_pod({"cpu": "1"})
    preset = types.SimpleNamespace(name="NVIDIA_VISIBLE_DEVICES", value="all")
    pod.spec.containers[0].env = [preset]

    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod

    assert pod.spec.containers[0].env == [preset]
    env = {e.name: e.value for e in pod.spec.containers[1].env}
    assert env == {"NVIDIA_VISIBLE_DEVICES": "void"}


async def test_unmanaged_gpu_flavor_is_left_to_the_scheduler(monkeypatch):
    # a GPU resource the gate does not know is neither checked nor hidden
    ns = load(monkeypatch)
    ns["observed_free_gpus"] = None  # calling it would raise TypeError
    pod = fake_pod({"nvidia.com/mig-3g.20gb": "1"})

    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod
    assert not getattr(pod.spec.containers[0], "env", None)


async def test_spawn_allowed_when_availability_unknown(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, None)
    pod = fake_pod({FULL: "1"})

    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod


async def test_admitted_spawn_reserves_the_gpu(monkeypatch):
    ns = load(monkeypatch)

    stub_prometheus(ns, ONE_FULL_FREE)

    # first spawn takes the last 40GB GPU ...
    pod = fake_pod({FULL: "1"})
    assert (
        await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner("alice"), pod) is pod
    )

    # ... so another user's immediate spawn is refused even though Prometheus
    # has not seen the first pod yet
    with pytest.raises(ns["GPUsUnavailableError"]):
        await ns["refuse_gpu_spawn_if_unavailable"](
            fake_spawner("bob"), fake_pod({FULL: "1"})
        )

    # and the form shows the reservation too
    profiles = await ns["profile_list_with_gpu_counts"](None)
    assert (
        gpu_label(profiles, FULL) == f"{plain(FULL)} — none available right now, {NOTE}"
    )


async def test_retry_after_cancelled_spawn_is_not_refused(monkeypatch):
    # a user who cancels a pending spawn and retries must not be blocked by
    # the reservation their own cancelled attempt left behind
    ns = load(monkeypatch)

    stub_prometheus(ns, ONE_FULL_FREE)

    pod = fake_pod({FULL: "1"})
    assert (
        await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner("alice"), pod) is pod
    )

    # ... they cancel (no post_stop_hook runs on an interrupted spawn) and
    # immediately retry, well inside GRANT_TTL
    retry = fake_pod({FULL: "1"})
    assert (
        await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner("alice"), retry)
        is retry
    )

    # and the retry holds exactly one reservation, not two
    assert ns["_grants_in_flight"](FULL) == 1


async def test_stop_releases_the_reservation(monkeypatch):
    ns = load(monkeypatch)

    stub_prometheus(ns, ONE_FULL_FREE)

    pod = fake_pod({FULL: "1"})
    assert (
        await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner("alice"), pod) is pod
    )
    assert ns["_grants_in_flight"](FULL) == 1

    ns["release_gpu_grants_on_stop"](fake_spawner("alice"))

    assert ns["_grants_in_flight"](FULL) == 0
    # the freed GPU is available to the next user straight away
    other = fake_pod({FULL: "1"})
    assert (
        await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner("bob"), other) is other
    )


def test_stop_releases_only_that_spawner(monkeypatch):
    ns = load(monkeypatch)
    ns["_recent_grants"].extend(
        [
            (ns["time"].monotonic(), FULL, "alice/"),
            (ns["time"].monotonic(), FULL, "bob/"),
        ]
    )

    ns["release_gpu_grants_on_stop"](fake_spawner("alice"))

    assert [g[2] for g in ns["_recent_grants"]] == ["bob/"]


async def test_refusal_counted_as_exhausted(monkeypatch):
    # nothing free in Prometheus: the facility really is full
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 5, FULL: 0, T4: 0})
    before = refusals(ns, FULL, "exhausted")

    with pytest.raises(ns["GPUsUnavailableError"]):
        await ns["refuse_gpu_spawn_if_unavailable"](
            fake_spawner("alice"), fake_pod({FULL: "1"})
        )

    assert refusals(ns, FULL, "exhausted") == before + 1


async def test_refusal_counted_as_reserved(monkeypatch):
    # Prometheus still sees a free GPU; only an in-flight admission took it.
    # Sustained counts here mean reservations are outliving their pods.
    ns = load(monkeypatch)

    stub_prometheus(ns, ONE_FULL_FREE)
    before = refusals(ns, FULL, "reserved")

    pod = fake_pod({FULL: "1"})
    assert (
        await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner("alice"), pod) is pod
    )

    with pytest.raises(ns["GPUsUnavailableError"]):
        await ns["refuse_gpu_spawn_if_unavailable"](
            fake_spawner("bob"), fake_pod({FULL: "1"})
        )

    assert refusals(ns, FULL, "reserved") == before + 1


# ── hub config wiring ─────────────────────────────────────────────────────────


def test_config_registers_callable_and_hook(monkeypatch):
    ns = load(monkeypatch)
    c = ns["c"]
    assert c["KubeSpawner"]["profile_list"] is ns["profile_list_with_gpu_counts"]
    assert c["KubeSpawner"]["modify_pod_hook"] is ns["refuse_gpu_spawn_if_unavailable"]
    assert c["KubeSpawner"]["post_stop_hook"] is ns["release_gpu_grants_on_stop"]


def test_profile_list_left_alone_without_static_list(monkeypatch):
    ns = load(monkeypatch, profile_list=[])
    # nothing to annotate, but the spawn gate still applies
    assert ns["c"]["KubeSpawner"]["profile_list"] == []
    assert (
        ns["c"]["KubeSpawner"]["modify_pod_hook"]
        is ns["refuse_gpu_spawn_if_unavailable"]
    )
