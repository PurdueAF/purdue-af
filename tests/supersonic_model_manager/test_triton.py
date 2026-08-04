"""Triton discovery, repository index aggregation and load/unload fan-out."""

import httpx
import pytest
import respx
from model_manager import triton
from model_manager.config import settings

SERVERS = ["10.0.0.1:8000", "10.0.0.2:8000"]


@pytest.fixture(autouse=True)
def static_servers(monkeypatch):
    monkeypatch.setattr(settings, "triton_discovery", "static")
    monkeypatch.setattr(settings, "triton_endpoints", list(SERVERS))
    monkeypatch.setattr(settings, "triton_timeout_s", 1)
    triton._control_capability.clear()
    yield
    triton._control_capability.clear()


def index_payload(*models):
    return [
        {"name": name, "version": "1", "state": state, "reason": ""}
        for name, state in models
    ]


def test_discovers_static_endpoints_and_defaults_the_port(monkeypatch):
    monkeypatch.setattr(settings, "triton_endpoints", ["10.0.0.9"])
    monkeypatch.setattr(settings, "triton_http_port", 8000)

    servers = triton.discover_servers()

    assert servers[0]["address"] == "10.0.0.9:8000"
    assert servers[0]["url"] == "http://10.0.0.9:8000"


@respx.mock
async def test_collect_state_merges_models_across_servers():
    respx.post(f"http://{SERVERS[0]}/v2/repository/index").mock(
        return_value=httpx.Response(
            200, json=index_payload(("a", "READY"), ("b", "READY"))
        )
    )
    respx.post(f"http://{SERVERS[1]}/v2/repository/index").mock(
        return_value=httpx.Response(
            200, json=index_payload(("a", "READY"), ("b", "UNAVAILABLE"))
        )
    )

    state = await triton.collect_state()

    assert [s["live"] for s in state["servers"]] == [True, True]
    assert set(state["models"]) == {"a", "b"}
    assert state["models"]["a"][SERVERS[0]]["state"] == "READY"
    assert state["models"]["b"][SERVERS[1]]["state"] == "UNAVAILABLE"


@respx.mock
async def test_unreachable_server_is_reported_without_losing_the_others():
    respx.post(f"http://{SERVERS[0]}/v2/repository/index").mock(
        return_value=httpx.Response(200, json=index_payload(("a", "READY")))
    )
    respx.post(f"http://{SERVERS[1]}/v2/repository/index").mock(
        side_effect=httpx.ConnectError("refused")
    )

    state = await triton.collect_state()

    live, dead = state["servers"]
    assert live["live"] is True
    assert dead["live"] is False and "ConnectError" in dead["error"]
    assert "a" in state["models"], "a live server's models must still be listed"


@respx.mock
async def test_load_fans_out_to_every_server():
    routes = [
        respx.post(f"http://{s}/v2/repository/models/mymodel/load").mock(
            return_value=httpx.Response(200, json={})
        )
        for s in SERVERS
    ]

    result = await triton.control_model("mymodel", "load")

    assert result["ok"] is True
    assert all(route.called for route in routes)
    assert len(result["results"]) == 2


@respx.mock
async def test_control_can_target_a_single_server():
    first = respx.post(f"http://{SERVERS[0]}/v2/repository/models/m/unload").mock(
        return_value=httpx.Response(200, json={})
    )
    second = respx.post(f"http://{SERVERS[1]}/v2/repository/models/m/unload").mock(
        return_value=httpx.Response(200, json={})
    )

    result = await triton.control_model("m", "unload", server_names=[SERVERS[0]])

    assert first.called and not second.called
    assert len(result["results"]) == 1


@respx.mock
async def test_partial_failure_is_not_reported_as_success():
    respx.post(f"http://{SERVERS[0]}/v2/repository/models/m/load").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.post(f"http://{SERVERS[1]}/v2/repository/models/m/load").mock(
        return_value=httpx.Response(500, text="boom")
    )

    result = await triton.control_model("m", "load")

    assert result["ok"] is False
    assert [r["ok"] for r in result["results"]] == [True, False]
    assert "boom" in result["error"]


@respx.mock
async def test_detects_servers_without_explicit_model_control():
    """Triton refuses runtime load/unload unless started with explicit mode."""
    for server in SERVERS:
        respx.post(f"http://{server}/v2/repository/models/m/load").mock(
            return_value=httpx.Response(
                400,
                text="explicit model load / unload is not allowed if polling is enabled",
            )
        )

    result = await triton.control_model("m", "load")

    assert result["ok"] is False
    assert all(r.get("controlDisabled") for r in result["results"])
    assert "--model-control-mode=explicit" in result["error"]
    assert triton._control_capability[SERVERS[0]] is False


async def test_control_without_servers_reports_cleanly(monkeypatch):
    monkeypatch.setattr(settings, "triton_endpoints", [])

    result = await triton.control_model("m", "load")

    assert result["ok"] is False
    assert "No Triton servers" in result["error"]


async def test_rejects_unknown_action():
    with pytest.raises(ValueError):
        await triton.control_model("m", "delete")


@respx.mock
async def test_index_hides_the_upload_staging_directory():
    """Triton lists every subdirectory of the repository as a model."""
    payload = index_payload(
        ("particlenet", "READY"),
        (".uploads", "UNAVAILABLE"),
        (".hidden", "UNAVAILABLE"),
    )
    for server in SERVERS:
        respx.post(f"http://{server}/v2/repository/index").mock(
            return_value=httpx.Response(200, json=payload)
        )

    state = await triton.collect_state()

    assert set(state["models"]) == {"particlenet"}
    assert [m["name"] for m in state["servers"][0]["models"]] == ["particlenet"]
