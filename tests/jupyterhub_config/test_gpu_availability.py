"""Tests for extraFiles/gpu-availability.py — live GPU counts + spawn refusal."""

import logging
import types

import pytest
from common import ConfigSink
from hub_helpers import load_snippet

SLICE = "nvidia.com/mig-1g.5gb"
FULL = "nvidia.com/mig-7g.40gb"


def gpu_profiles():
    """Static profileList mirroring the GPU-relevant shape of values.yaml."""
    return [
        {
            "display_name": "Purdue AF",
            "profile_options": {
                "0-cpu": {
                    "display_name": "CPUs",
                    "choices": {
                        "1": {
                            "display_name": "4",
                            "kubespawner_override": {"cpu_guarantee": 4},
                        },
                    },
                },
                "1-gpu": {
                    "display_name": "GPUs",
                    "choices": {
                        "1": {
                            "display_name": "0",
                            "kubespawner_override": {
                                "extra_resource_limits": {SLICE: 0}
                            },
                        },
                        "2": {
                            "display_name": "1 A100 GPU slice (5GB)",
                            "kubespawner_override": {
                                "extra_resource_limits": {SLICE: 1}
                            },
                        },
                        "3": {
                            "display_name": "1 full A100 GPU (40GB) - subject to availability",
                            "kubespawner_override": {
                                "extra_resource_limits": {FULL: 1}
                            },
                        },
                    },
                },
            },
        },
    ]


def load(monkeypatch, profile_list=...):
    sink = ConfigSink()
    sink.KubeSpawner.profile_list = (
        gpu_profiles() if profile_list is ... else profile_list
    )
    return load_snippet("gpu-availability.py", monkeypatch, extra_globals={"c": sink})


def set_free(ns, value):
    """Stub the availability lookup the snippet's callables resolve at runtime."""

    async def _free(use_cache=True):
        return value

    ns["free_gpus"] = _free


def gpu_choices(profiles):
    return profiles[0]["profile_options"]["1-gpu"]["choices"]


def fake_pod(limits):
    main = types.SimpleNamespace(resources=types.SimpleNamespace(limits=limits))
    sidecar = types.SimpleNamespace(resources=None)  # af-pod-monitor has no limits
    return types.SimpleNamespace(spec=types.SimpleNamespace(containers=[main, sidecar]))


def fake_spawner():
    return types.SimpleNamespace(log=logging.getLogger("test-spawner"))


# ── profile form annotation ───────────────────────────────────────────────────


async def test_form_shows_live_counts(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 12, FULL: 2})

    profiles = await ns["profile_list_with_gpu_counts"](None)

    choices = gpu_choices(profiles)
    assert choices["2"]["display_name"] == "1 A100 GPU slice (5GB) — 12 available now"
    # the static "subject to availability" note is replaced by the live count
    assert choices["3"]["display_name"] == "1 full A100 GPU (40GB) — 2 available now"
    # zero-GPU choice and non-GPU options stay untouched
    assert choices["1"]["display_name"] == "0"
    assert (
        profiles[0]["profile_options"]["0-cpu"]["choices"]["1"]["display_name"] == "4"
    )


async def test_form_marks_exhausted_flavor(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 3, FULL: 0})

    choices = gpu_choices(await ns["profile_list_with_gpu_counts"](None))

    assert choices["2"]["display_name"] == "1 A100 GPU slice (5GB) — 3 available now"
    assert (
        choices["3"]["display_name"]
        == "1 full A100 GPU (40GB) — none available right now"
    )


async def test_form_untouched_when_availability_unknown(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, None)

    profiles = await ns["profile_list_with_gpu_counts"](None)

    assert profiles == gpu_profiles()


async def test_static_profile_list_is_not_mutated(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 1, FULL: 1})

    await ns["profile_list_with_gpu_counts"](None)

    assert ns["_static_profile_list"] == gpu_profiles()


# ── availability lookup ───────────────────────────────────────────────────────


async def test_get_free_gpus_subtracts_usage(monkeypatch):
    ns = load(monkeypatch)

    async def prom(query):
        if "kube_node_status_allocatable" in query:
            return {"nvidia_com_mig_1g_5gb": 21.0, "nvidia_com_mig_7g_40gb": 2.0}
        return {"nvidia_com_mig_1g_5gb": 20.0}  # no 40GB pods running

    ns["_prom_query"] = prom

    assert await ns["get_free_gpus"]() == {SLICE: 1, FULL: 2}


async def test_get_free_gpus_clamps_at_zero(monkeypatch):
    ns = load(monkeypatch)

    async def prom(query):
        if "kube_node_status_allocatable" in query:
            return {"nvidia_com_mig_1g_5gb": 1.0}  # no 40GB capacity at all
        return {"nvidia_com_mig_1g_5gb": 5.0}  # over-reported usage

    ns["_prom_query"] = prom

    assert await ns["get_free_gpus"]() == {SLICE: 0, FULL: 0}


async def test_get_free_gpus_unknown_on_query_failure(monkeypatch):
    ns = load(monkeypatch)

    async def prom(query):
        raise OSError("connection refused")

    ns["_prom_query"] = prom

    assert await ns["get_free_gpus"]() is None


async def test_get_free_gpus_unknown_when_metrics_missing(monkeypatch):
    ns = load(monkeypatch)

    async def prom(query):
        return {}

    ns["_prom_query"] = prom

    assert await ns["get_free_gpus"]() is None


# ── spawn-time enforcement ────────────────────────────────────────────────────


async def test_spawn_refused_when_flavor_exhausted(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 5, FULL: 0})

    with pytest.raises(ns["GPUsUnavailableError"], match="full A100 GPUs"):
        await ns["refuse_gpu_spawn_if_unavailable"](
            fake_spawner(), fake_pod({FULL: "1"})
        )


async def test_spawn_allowed_when_available(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, {SLICE: 5, FULL: 1})
    pod = fake_pod({FULL: "1", "cpu": "256", "memory": "256G"})

    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod


async def test_non_gpu_spawn_skips_availability_check(monkeypatch):
    ns = load(monkeypatch)

    async def boom(use_cache=True):
        raise AssertionError("availability should not be queried")

    ns["free_gpus"] = boom

    pod = fake_pod({"cpu": "256", "memory": "256G"})
    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod

    # a zero-valued GPU limit (the "0 GPUs" choice) is not a GPU request
    pod = fake_pod({SLICE: 0})
    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod


async def test_spawn_allowed_when_availability_unknown(monkeypatch):
    ns = load(monkeypatch)
    set_free(ns, None)
    pod = fake_pod({FULL: "1"})

    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod


async def test_admitted_spawn_reserves_the_gpu(monkeypatch):
    ns = load(monkeypatch)

    async def one_full_free():
        return {SLICE: 0, FULL: 1}

    ns["get_free_gpus"] = one_full_free

    # first spawn takes the last 40GB GPU ...
    pod = fake_pod({FULL: "1"})
    assert await ns["refuse_gpu_spawn_if_unavailable"](fake_spawner(), pod) is pod

    # ... so an immediate second spawn is refused even though Prometheus
    # has not seen the first pod yet
    with pytest.raises(ns["GPUsUnavailableError"]):
        await ns["refuse_gpu_spawn_if_unavailable"](
            fake_spawner(), fake_pod({FULL: "1"})
        )

    # and the form shows the reservation too
    choices = gpu_choices(await ns["profile_list_with_gpu_counts"](None))
    assert (
        choices["3"]["display_name"]
        == "1 full A100 GPU (40GB) — none available right now"
    )


# ── hub config wiring ─────────────────────────────────────────────────────────


def test_config_registers_callable_and_hook(monkeypatch):
    ns = load(monkeypatch)
    c = ns["c"]
    assert c["KubeSpawner"]["profile_list"] is ns["profile_list_with_gpu_counts"]
    assert c["KubeSpawner"]["modify_pod_hook"] is ns["refuse_gpu_spawn_if_unavailable"]


def test_profile_list_left_alone_without_static_list(monkeypatch):
    ns = load(monkeypatch, profile_list=[])
    # nothing to annotate, but the spawn gate still applies
    assert ns["c"]["KubeSpawner"]["profile_list"] == []
    assert (
        ns["c"]["KubeSpawner"]["modify_pod_hook"]
        is ns["refuse_gpu_spawn_if_unavailable"]
    )
