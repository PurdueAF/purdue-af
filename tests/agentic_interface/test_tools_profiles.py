"""Tests for tools/profiles.py — values.yaml parsing, slugs, and caching."""

import asyncio

import pytest
import respx
from agentic_helpers import failure, register_tools
from tools import profiles

VALUES_YAML = """
singleuser:
  profileList:
    - display_name: "Purdue AF 0.12.4 (Stable)"
      default: true
      description: "<b>Recommended.</b> Stable image."
      profile_options:
        0-cpu:
          display_name: CPU count
          choices:
            "1":
              display_name: "4 cores"
              default: true
            "2":
              display_name: "16 cores"
        3-interface:
          display_name: Interface
          choices:
            "1": "JupyterLab"
            "2": "VS Code"
    - display_name: "Pre-release"
      description: ""
"""


@pytest.fixture(autouse=True)
def clear_profile_cache():
    def reset():
        profiles._fresh.clear()
        profiles._last_good = []
        profiles._last_error = None

    reset()
    yield
    reset()


# ── _slug ─────────────────────────────────────────────────────────────────────


def test_slug_normalisation():
    assert profiles._slug("Purdue AF 0.12.4 (Stable)") == "purdue-af-0-12-4-stable"
    assert profiles._slug("  Already-Slugged ") == "already-slugged"


# ── _parse_profiles ───────────────────────────────────────────────────────────


def test_parse_profiles_full():
    parsed = profiles._parse_profiles(VALUES_YAML)
    assert len(parsed) == 2

    stable = parsed[0]
    assert stable["default"] is True
    assert stable["slug"] == "purdue-af-0-12-4-stable"
    assert stable["description"] == "Recommended. Stable image."  # HTML stripped
    assert stable["options"]["0-cpu"]["choices"]["1"] == "4 cores (default)"
    assert stable["options"]["3-interface"]["choices"]["2"] == "VS Code"

    assert parsed[1]["default"] is False
    assert parsed[1]["options"] == {}


GPU_VALUES_YAML = """
singleuser:
  profileList:
    - display_name: "Stable"
      default: true
      profile_options:
        1-gpu:
          display_name: GPUs
          choices:
            "1":
              display_name: "0"
              kubespawner_override:
                extra_resource_limits:
                  nvidia.com/mig-1g.5gb: 0
            "2":
              display_name: "1 A100 GPU slice (5GB)"
              kubespawner_override:
                extra_resource_limits:
                  nvidia.com/mig-1g.5gb: 1
            "3":
              display_name: "1 full A100 GPU (40GB) - subject to availability"
              kubespawner_override:
                extra_resource_limits:
                  nvidia.com/mig-7g.40gb: 1
            "4":
              display_name: "1 NVIDIA T4 GPU (16GB)"
              kubespawner_override:
                extra_resource_limits:
                  nvidia.com/gpu: 1
"""


def test_parse_profiles_gpu_map():
    parsed = profiles._parse_profiles(GPU_VALUES_YAML)
    gpu_opt = parsed[0]["options"]["1-gpu"]
    # Only choices requesting > 0 GPUs are mapped; the "0" choice is omitted.
    assert gpu_opt["gpu"] == {
        "2": "nvidia.com/mig-1g.5gb",
        "3": "nvidia.com/mig-7g.40gb",
        "4": "nvidia.com/gpu",
    }
    # Non-GPU options carry no "gpu" key.
    assert "gpu" not in profiles._parse_profiles(VALUES_YAML)[0]["options"]["0-cpu"]


def test_parse_profiles_invalid_yaml():
    assert profiles._parse_profiles("][ not yaml") == []


def test_parse_profiles_missing_list():
    assert profiles._parse_profiles("singleuser: {}") == []


# ── find_profile ──────────────────────────────────────────────────────────────


def test_find_profile_by_slug_and_name():
    parsed = profiles._parse_profiles(VALUES_YAML)
    assert profiles.find_profile(parsed, "purdue-af-0-12-4-stable") is parsed[0]
    assert profiles.find_profile(parsed, "PURDUE af 0.12.4 (stable)") is parsed[0]
    assert profiles.find_profile(parsed, "pre-release") is parsed[1]
    assert profiles.find_profile(parsed, "ghost") is None


# ── get_profiles caching ──────────────────────────────────────────────────────


async def test_get_profiles_caches(monkeypatch):
    calls = 0

    async def fake_read():
        nonlocal calls
        calls += 1
        return VALUES_YAML

    monkeypatch.setattr(profiles, "_read_configmap", fake_read)

    first = await profiles.get_profiles()
    second = await profiles.get_profiles()
    assert first == second
    assert calls == 1  # second call served from cache


async def test_get_profiles_force_refresh(monkeypatch):
    calls = 0

    async def fake_read():
        nonlocal calls
        calls += 1
        return VALUES_YAML

    monkeypatch.setattr(profiles, "_read_configmap", fake_read)

    await profiles.get_profiles()
    await profiles.get_profiles(force=True)
    assert calls == 2


async def test_get_profiles_concurrent_misses_fetch_once(monkeypatch):
    """Concurrent cache misses are single-flighted — no ConfigMap dogpile."""
    calls = 0

    async def slow_read():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)  # yield so the other callers reach the lock
        return VALUES_YAML

    monkeypatch.setattr(profiles, "_read_configmap", slow_read)

    results = await asyncio.gather(*(profiles.get_profiles() for _ in range(5)))
    assert calls == 1
    assert all(r == results[0] for r in results)


async def test_get_profiles_stale_cache_fallback(monkeypatch):
    async def good_read():
        return VALUES_YAML

    async def broken_read():
        return None

    monkeypatch.setattr(profiles, "_read_configmap", good_read)
    cached = await profiles.get_profiles()

    # Expire the cache, then break the source: stale data is better than none.
    profiles._fresh.clear()
    monkeypatch.setattr(profiles, "_read_configmap", broken_read)

    assert await profiles.get_profiles() == cached


async def test_get_profiles_no_source_no_cache(monkeypatch):
    async def broken_read():
        return None

    monkeypatch.setattr(profiles, "_read_configmap", broken_read)
    assert await profiles.get_profiles() == []


# ── list_af_profiles tool ─────────────────────────────────────────────────────


async def test_list_profiles_tool_renders(monkeypatch):
    async def fake_read():
        return VALUES_YAML

    monkeypatch.setattr(profiles, "_read_configmap", fake_read)

    tools = register_tools(profiles).tools
    out = await tools["list_af_profiles"]()

    assert "# 2 available profile(s)" in out
    assert "*(default)*" in out
    assert 'slug: `"purdue-af-0-12-4-stable"`' in out
    assert '`"2"` → VS Code' in out


async def test_list_profiles_tool_shows_gpu_availability(monkeypatch):
    async def fake_read():
        return GPU_VALUES_YAML

    async def fake_free():
        return {"nvidia.com/mig-1g.5gb": 4, "nvidia.com/mig-7g.40gb": 0}

    monkeypatch.setattr(profiles, "_read_configmap", fake_read)
    monkeypatch.setattr(profiles, "free_gpus", fake_free)

    tools = register_tools(profiles).tools
    out = await tools["list_af_profiles"]()

    assert "1 A100 GPU slice (5GB) — 4 available now" in out
    # Exhausted flavor is shown but flagged, and the static hedge is dropped.
    assert "1 full A100 GPU (40GB) — none available right now (do not select)" in out
    assert "subject to availability" not in out


async def test_list_profiles_tool_gpu_availability_unknown(monkeypatch):
    async def fake_read():
        return GPU_VALUES_YAML

    async def fake_free():
        return None  # Prometheus unreachable

    monkeypatch.setattr(profiles, "_read_configmap", fake_read)
    monkeypatch.setattr(profiles, "free_gpus", fake_free)

    tools = register_tools(profiles).tools
    out = await tools["list_af_profiles"]()

    # Falls back to the static labels unchanged.
    assert "available now" not in out
    assert "1 A100 GPU slice (5GB)" in out


async def test_list_profiles_tool_unavailable(monkeypatch):
    async def broken_read():
        return None

    monkeypatch.setattr(profiles, "_read_configmap", broken_read)

    tools = register_tools(profiles).tools
    out = await failure(tools["list_af_profiles"]())
    assert "Could not read profile list" in out


# ── failure reasons ───────────────────────────────────────────────────────────
#
# An empty profile list always comes with a reason a user can act on (or at
# least report), never a bare "misconfigured".

CM_URL = (
    f"{profiles._K8S_API}/api/v1/namespaces/{profiles._NAMESPACE}"
    f"/configmaps/{profiles._CONFIGMAP}"
)


@pytest.fixture
def in_cluster(monkeypatch, tmp_path):
    """Pretend to run inside Kubernetes, with a plain (mockable) HTTP client."""
    import httpx

    token = tmp_path / "token"
    token.write_text("sa-token")
    monkeypatch.setattr(profiles, "_TOKEN_PATH", str(token))
    monkeypatch.setattr(
        profiles, "shared_client", lambda name, **kwargs: httpx.AsyncClient()
    )


async def test_read_configmap_outside_kubernetes_explains(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, "_TOKEN_PATH", str(tmp_path / "missing"))
    assert await profiles._read_configmap() is None
    assert "not running inside Kubernetes" in profiles.profiles_error()


@pytest.mark.parametrize(
    "status, expected",
    [(403, "RBAC"), (404, "does not exist"), (500, "returned HTTP 500")],
)
@respx.mock
async def test_read_configmap_http_failures_are_explained(in_cluster, status, expected):
    respx.get(CM_URL).respond(status)
    assert await profiles._read_configmap() is None
    assert expected in profiles.profiles_error()


@respx.mock
async def test_read_configmap_unreachable_missing_values_and_success(in_cluster):
    from httpx import ConnectError

    respx.get(CM_URL).mock(side_effect=ConnectError("down"))
    assert await profiles._read_configmap() is None
    assert profiles.profiles_error().startswith("Kubernetes API unreachable")

    respx.get(CM_URL).respond(200, json={"data": {}})
    assert await profiles._read_configmap() is None
    assert "no values.yaml" in profiles.profiles_error()

    respx.get(CM_URL).respond(200, json={"data": {"values.yaml": VALUES_YAML}})
    assert await profiles._read_configmap() == VALUES_YAML
    assert profiles.profiles_error() is None


def test_parse_profiles_tolerates_non_mapping_yaml():
    assert profiles._parse_profiles("") == []
    assert profiles._parse_profiles("- a\n- b\n") == []
    assert profiles._parse_profiles("singleuser: [1, 2]\n") == []
    assert profiles._parse_profiles("singleuser:\n  profileList:\n    - 7\n") == []


async def test_get_profiles_unparseable_config_has_a_reason(monkeypatch):
    async def junk():
        return "singleuser: {}\n"

    monkeypatch.setattr(profiles, "_read_configmap", junk)
    assert await profiles.get_profiles() == []
    assert "profileList" in profiles.profiles_error()


async def test_list_profiles_tool_reports_the_reason(monkeypatch):
    async def broken_read():
        profiles._last_error = "Kubernetes API unreachable — connection refused"
        return None

    monkeypatch.setattr(profiles, "_read_configmap", broken_read)
    out = await failure(register_tools(profiles).tools["list_af_profiles"]())
    assert out.startswith(
        "Could not read profile list — Kubernetes API unreachable — connection refused."
    )
    assert "use_defaults=True" in out


async def test_list_profiles_tool_names_why_gpu_counts_are_missing(monkeypatch):
    async def fake_read():
        return GPU_VALUES_YAML

    async def fake_free():
        return None

    monkeypatch.setattr(profiles, "_read_configmap", fake_read)
    monkeypatch.setattr(profiles, "free_gpus", fake_free)
    monkeypatch.setattr(
        profiles, "gpu_error", lambda: "Prometheus is unreachable — connection refused"
    )
    out = await register_tools(profiles).tools["list_af_profiles"]()
    assert (
        "live GPU availability is unknown right now (Prometheus is unreachable — "
        "connection refused)" in out
    )
