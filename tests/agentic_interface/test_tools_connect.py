"""Tests for tools/connect.py — SSH setup rendering and key injection."""

import sys
import types

import pytest
from agentic_helpers import register_tools
from tools import connect

PUBKEY = "ssh-ed25519 AAAATESTKEY purdue-af-agentic"


# ── _render_ssh_setup ─────────────────────────────────────────────────────────


def test_ssh_setup_bakes_in_user_and_host():
    out = connect._render_ssh_setup("alice")
    assert "User alice" in out
    assert f"HostName {connect._SSH_HOST}" in out
    assert "Host PurdueAF" in out
    assert "ControlMaster auto" in out
    assert "<< 'EOF'" in out  # quoted heredoc: no shell expansion


# ── prepare_ssh_connection ────────────────────────────────────────────────────


async def test_prepare_with_running_pod(user_ctx):
    tools = register_tools(connect).tools
    out = await tools["prepare_ssh_connection"]()

    assert "User alice" in out
    assert "ALREADY_CONNECTED" in out
    assert "no running session detected" not in out


async def test_prepare_without_pod_warns(podless_user_ctx):
    tools = register_tools(connect).tools
    out = await tools["prepare_ssh_connection"]()
    assert "no running session detected" in out


# ── connect_to_session (injection mocked) ─────────────────────────────────────


async def test_connect_requires_pod(podless_user_ctx):
    tools = register_tools(connect).tools
    out = await tools["connect_to_session"](PUBKEY)
    assert "No running session found" in out


@pytest.mark.parametrize(
    ("inject_result", "expected"),
    [
        ("EXISTS", "already present"),
        ("ADDED", "Key added"),
    ],
)
async def test_connect_reports_key_status(
    user_ctx, monkeypatch, inject_result, expected
):
    async def fake_inject(pod_name, username, public_key):
        assert pod_name == "purdue-af-alice"
        assert username == "alice"
        assert public_key == PUBKEY
        return inject_result

    monkeypatch.setattr(connect, "_check_and_inject", fake_inject)

    tools = register_tools(connect).tools
    out = await tools["connect_to_session"](PUBKEY)

    assert expected in out
    assert 'ssh PurdueAF "hostname"' in out


async def test_connect_propagates_errors(user_ctx, monkeypatch):
    async def fake_inject(pod_name, username, public_key):
        return "Error: could not verify pod ownership — contact AF support"

    monkeypatch.setattr(connect, "_check_and_inject", fake_inject)

    tools = register_tools(connect).tools
    out = await tools["connect_to_session"](PUBKEY)
    assert out.startswith("Error:")


# ── _check_and_inject ownership gate (kubernetes mocked) ──────────────────────


class FakePod:
    def __init__(self, labels):
        self.metadata = types.SimpleNamespace(labels=labels)


def install_fake_kubernetes(monkeypatch, pod_labels, exec_output="ADDED"):
    """Install a minimal fake `kubernetes` package into sys.modules."""
    exec_calls = []

    class FakeCoreV1Api:
        def read_namespaced_pod(self, name, namespace):
            return FakePod(pod_labels)

        def connect_get_namespaced_pod_exec(self, *a, **kw):  # pragma: no cover
            raise AssertionError("exec must go through kubernetes.stream")

    def fake_stream(fn, **kwargs):
        exec_calls.append(kwargs)
        return exec_output + "\n"

    kubernetes = types.ModuleType("kubernetes")
    kubernetes.client = types.SimpleNamespace(CoreV1Api=FakeCoreV1Api)
    kubernetes.config = types.SimpleNamespace(load_incluster_config=lambda: None)
    stream_mod = types.ModuleType("kubernetes.stream")
    stream_mod.stream = fake_stream

    monkeypatch.setitem(sys.modules, "kubernetes", kubernetes)
    monkeypatch.setitem(sys.modules, "kubernetes.stream", stream_mod)
    return exec_calls


async def test_inject_denies_foreign_pod(monkeypatch):
    exec_calls = install_fake_kubernetes(
        monkeypatch, pod_labels={"username_unescaped": "mallory"}
    )

    out = await connect._check_and_inject("purdue-af-mallory", "alice", PUBKEY)

    assert "access denied" in out
    assert exec_calls == []  # exec never reached


async def test_inject_allows_own_pod_and_execs(monkeypatch):
    exec_calls = install_fake_kubernetes(
        monkeypatch, pod_labels={"username_unescaped": "alice"}, exec_output="ADDED"
    )

    out = await connect._check_and_inject("purdue-af-alice", "alice", PUBKEY)

    assert out == "ADDED"
    assert len(exec_calls) == 1
    assert exec_calls[0]["container"] == connect._CONTAINER
    # the key travels base64-encoded inside the script
    assert "base64 -d" in exec_calls[0]["command"][2]


async def test_inject_handles_missing_labels(monkeypatch):
    install_fake_kubernetes(monkeypatch, pod_labels=None)

    out = await connect._check_and_inject("purdue-af-alice", "alice", PUBKEY)
    assert "access denied" in out
