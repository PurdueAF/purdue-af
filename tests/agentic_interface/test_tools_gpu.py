"""Tests for tools/gpu.py — live GPU availability + choice annotation/filtering."""

import pytest
import respx
from tools import gpu

CHOICES = {
    "1": "0",
    "2": "1 A100 GPU slice (5GB)",
    "3": "1 full A100 GPU (40GB) - subject to availability",
}
GPU_MAP = {"2": "nvidia.com/mig-1g.5gb", "3": "nvidia.com/mig-7g.40gb"}


@pytest.fixture(autouse=True)
def clear_gpu_cache():
    gpu._cache = (0.0, None)
    yield
    gpu._cache = (0.0, None)


# ── apply_availability ────────────────────────────────────────────────────────


def test_apply_availability_annotates_and_hides():
    free = {"nvidia.com/mig-1g.5gb": 3, "nvidia.com/mig-7g.40gb": 0}
    labels, keys = gpu.apply_availability(CHOICES, GPU_MAP, free)

    # "0" (no GPU) always kept and untouched.
    assert keys == ["1", "2"]  # flavor with 0 free is hidden
    assert labels["1"] == "0"
    assert labels["2"] == "1 A100 GPU slice (5GB) — 3 available now"
    assert "3" not in labels


def test_apply_availability_strips_subject_to_availability_suffix():
    free = {"nvidia.com/mig-1g.5gb": 0, "nvidia.com/mig-7g.40gb": 2}
    labels, keys = gpu.apply_availability(CHOICES, GPU_MAP, free)

    assert keys == ["1", "3"]
    assert labels["3"] == "1 full A100 GPU (40GB) — 2 available now"


def test_apply_availability_unknown_keeps_everything():
    labels, keys = gpu.apply_availability(CHOICES, GPU_MAP, None)
    assert keys == ["1", "2", "3"]
    assert labels == CHOICES  # untouched (fail-open)


# ── free_gpus ─────────────────────────────────────────────────────────────────


def _prom_result(resource, value):
    return {"metric": {"resource": resource}, "value": [0, str(value)]}


@respx.mock
async def test_free_gpus_subtracts_used_from_allocatable():
    def responder(request):
        import httpx as _httpx

        query = request.url.params["query"]
        if "allocatable" in query:
            data = [
                _prom_result("nvidia_com_mig_1g_5gb", 8),
                _prom_result("nvidia_com_mig_7g_40gb", 2),
            ]
        else:
            data = [_prom_result("nvidia_com_mig_1g_5gb", 5)]
        return _httpx.Response(200, json={"data": {"result": data}})

    respx.get(f"{gpu.PROMETHEUS_URL}/api/v1/query").mock(side_effect=responder)

    free = await gpu.free_gpus()
    assert free == {
        "nvidia.com/mig-1g.5gb": 3,  # 8 - 5
        "nvidia.com/mig-7g.40gb": 2,  # 2 - 0
    }


@respx.mock
async def test_free_gpus_returns_none_on_error():
    respx.get(f"{gpu.PROMETHEUS_URL}/api/v1/query").respond(500)
    assert await gpu.free_gpus() is None


@respx.mock
async def test_free_gpus_none_when_no_allocatable_metrics():
    respx.get(f"{gpu.PROMETHEUS_URL}/api/v1/query").respond(
        200, json={"data": {"result": []}}
    )
    assert await gpu.free_gpus() is None
