"""Tests for tools/profiles.py — values.yaml parsing, slugs, and caching."""

import pytest
from agentic_helpers import register_tools
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
    profiles._cache = None
    yield
    profiles._cache = None


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


async def test_get_profiles_stale_cache_fallback(monkeypatch):
    async def good_read():
        return VALUES_YAML

    async def broken_read():
        return None

    monkeypatch.setattr(profiles, "_read_configmap", good_read)
    cached = await profiles.get_profiles()

    # Expire the cache, then break the source: stale data is better than none.
    expiry, data = profiles._cache
    profiles._cache = (expiry - 10_000, data)
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


async def test_list_profiles_tool_unavailable(monkeypatch):
    async def broken_read():
        return None

    monkeypatch.setattr(profiles, "_read_configmap", broken_read)

    tools = register_tools(profiles).tools
    out = await tools["list_af_profiles"]()
    assert "Could not read profile list" in out
