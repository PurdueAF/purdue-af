"""Tests for docker/purdue-af/jupyter/jupyter_server_config.py.

The file is exec'd by `jupyter server` with `get_config()` in scope; the
load_jupyter_config helper in conftest replicates that, so we test the file
exactly as it ships in the image.
"""

import logging
import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest
from script_helpers import load_jupyter_config


@pytest.fixture(autouse=True)
def _restore_tornado_app_logger():
    """Each exec adds a filter to the tornado.application logger — undo it."""
    logger = logging.getLogger("tornado.application")
    before = list(logger.filters)
    yield
    logger.filters[:] = before


@pytest.fixture
def fake_tornado(monkeypatch):
    """Install a fake tornado.websocket so the ping-units patch applies."""

    class Params:
        def __init__(self, ping_interval=None, ping_timeout=None):
            self.ping_interval = ping_interval
            self.ping_timeout = ping_timeout

    class Protocol:
        def __init__(self, params=None):
            self.params = params

    class WebSocketHandler:
        protocol = None  # set per test

        def get_websocket_protocol(self):
            return type(self).protocol

    ws_mod = types.ModuleType("tornado.websocket")
    ws_mod.WebSocketHandler = WebSocketHandler
    tornado_mod = types.ModuleType("tornado")
    tornado_mod.websocket = ws_mod
    monkeypatch.setitem(sys.modules, "tornado", tornado_mod)
    monkeypatch.setitem(sys.modules, "tornado.websocket", ws_mod)
    return types.SimpleNamespace(
        handler_cls=WebSocketHandler, Params=Params, Protocol=Protocol
    )


class TestBaseConfig:
    def test_server_settings(self, monkeypatch, tmp_path):
        _, c = load_jupyter_config(monkeypatch, tmp_path)

        assert c["ServerApp"]["ip"] == "0.0.0.0"
        assert c["ServerApp"]["open_browser"] is False
        assert c["ServerApp"]["log_level"] == "WARN"
        headers = c["ServerApp"]["tornado_settings"]["headers"]
        assert "clipboard-read=(self)" in headers["Permissions-Policy"]

    def test_contents_and_kernel_settings(self, monkeypatch, tmp_path):
        _, c = load_jupyter_config(monkeypatch, tmp_path)

        assert c["FileContentsManager"]["allow_hidden"] is True
        assert c["FileContentsManager"]["delete_to_trash"] is False
        assert c["InlineBackend"]["figure_formats"] == {"png", "jpeg", "svg", "pdf"}
        assert c["KernelSpecManager"]["ensure_native_kernel"] is False

    def test_no_cert_or_umask_side_effects_by_default(self, monkeypatch, tmp_path):
        umask_calls = []
        monkeypatch.setattr(os, "umask", lambda v: umask_calls.append(v))
        _, c = load_jupyter_config(monkeypatch, tmp_path)

        assert "certfile" not in c["ServerApp"]
        assert umask_calls == []
        assert not (tmp_path / "notebook.pem").exists()


class TestXSRFNoiseFilter:
    def test_filter_drops_xsrf_skip_lines_only(self, monkeypatch, tmp_path):
        ns, _ = load_jupyter_config(monkeypatch, tmp_path)
        noise_filter = ns["_SuppressXSRFSkipNoise"]()

        def record(msg):
            return logging.LogRecord("t", logging.WARNING, "f", 1, msg, None, None)

        assert not noise_filter.filter(
            record("Skipping XSRF check for insecure request /api")
        )
        assert noise_filter.filter(record("some other warning"))

    def test_filter_installed_on_tornado_application_logger(
        self, monkeypatch, tmp_path
    ):
        ns, _ = load_jupyter_config(monkeypatch, tmp_path)
        logger = logging.getLogger("tornado.application")
        assert any(isinstance(f, ns["_SuppressXSRFSkipNoise"]) for f in logger.filters)


class TestGenCert:
    def _setup(self, monkeypatch, tmp_path, create_pem=True):
        conda_dir = tmp_path / "conda"
        (conda_dir / "ssl").mkdir(parents=True)
        data_dir = tmp_path / "data"
        calls = []

        def fake_check_call(cmd):
            calls.append(cmd)
            if create_pem:
                keyout = next(a for a in cmd if a.startswith("-keyout="))
                Path(keyout.split("=", 1)[1]).touch()

        monkeypatch.setattr(subprocess, "check_call", fake_check_call)
        return conda_dir, data_dir, calls

    def test_generates_cert_and_restricts_access(self, monkeypatch, tmp_path):
        conda_dir, data_dir, calls = self._setup(monkeypatch, tmp_path)
        ns, c = load_jupyter_config(
            monkeypatch,
            data_dir,
            env={"GEN_CERT": "1", "CONDA_DIR": str(conda_dir)},
        )

        pem = data_dir / "notebook.pem"
        assert c["ServerApp"]["certfile"] == str(pem)
        assert len(calls) == 1
        assert calls[0][0:2] == ["openssl", "req"]
        assert f"-keyout={pem}" in calls[0]
        assert f"-out={pem}" in calls[0]
        assert stat.S_IMODE(pem.stat().st_mode) == 0o600

    def test_writes_openssl_cnf_when_missing(self, monkeypatch, tmp_path):
        conda_dir, data_dir, _ = self._setup(monkeypatch, tmp_path)
        ns, _ = load_jupyter_config(
            monkeypatch,
            data_dir,
            env={"GEN_CERT": "1", "CONDA_DIR": str(conda_dir)},
        )
        cnf = conda_dir / "ssl" / "openssl.cnf"
        assert cnf.read_text() == ns["OPENSSL_CONFIG"]

    def test_keeps_existing_openssl_cnf(self, monkeypatch, tmp_path):
        conda_dir, data_dir, _ = self._setup(monkeypatch, tmp_path)
        cnf = conda_dir / "ssl" / "openssl.cnf"
        cnf.write_text("# pre-existing\n")
        load_jupyter_config(
            monkeypatch,
            data_dir,
            env={"GEN_CERT": "1", "CONDA_DIR": str(conda_dir)},
        )
        assert cnf.read_text() == "# pre-existing\n"


class TestNBUmask:
    def test_umask_applied_as_octal(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(os, "umask", lambda v: calls.append(v))
        load_jupyter_config(monkeypatch, tmp_path, env={"NB_UMASK": "022"})
        assert calls == [0o022]


class TestWebSocketPingUnitsPatch:
    def _wrapped_protocol(self, monkeypatch, tmp_path, fake_tornado, params):
        load_jupyter_config(monkeypatch, tmp_path)
        fake_tornado.handler_cls.protocol = fake_tornado.Protocol(params)
        return fake_tornado.handler_cls().get_websocket_protocol()

    def test_millisecond_values_converted_to_seconds(
        self, monkeypatch, tmp_path, fake_tornado
    ):
        proto = self._wrapped_protocol(
            monkeypatch, tmp_path, fake_tornado, fake_tornado.Params(30000, 90000)
        )
        assert proto.params.ping_interval == 30.0
        # converted, then clamped to the interval
        assert proto.params.ping_timeout == 30.0

    def test_second_values_left_alone(self, monkeypatch, tmp_path, fake_tornado):
        proto = self._wrapped_protocol(
            monkeypatch, tmp_path, fake_tornado, fake_tornado.Params(30, 20)
        )
        assert proto.params.ping_interval == 30
        assert proto.params.ping_timeout == 20

    def test_none_interval_converts_timeout_only(
        self, monkeypatch, tmp_path, fake_tornado
    ):
        proto = self._wrapped_protocol(
            monkeypatch, tmp_path, fake_tornado, fake_tornado.Params(None, 90000)
        )
        assert proto.params.ping_interval is None
        assert proto.params.ping_timeout == 90.0

    def test_none_protocol_passthrough(self, monkeypatch, tmp_path, fake_tornado):
        load_jupyter_config(monkeypatch, tmp_path)
        fake_tornado.handler_cls.protocol = None
        assert fake_tornado.handler_cls().get_websocket_protocol() is None

    def test_protocol_without_params_passthrough(
        self, monkeypatch, tmp_path, fake_tornado
    ):
        proto = self._wrapped_protocol(monkeypatch, tmp_path, fake_tornado, None)
        assert proto.params is None

    def test_config_loads_without_tornado(self, monkeypatch, tmp_path):
        # Simulate tornado being absent: import raises, patch is skipped.
        monkeypatch.setitem(sys.modules, "tornado.websocket", None)
        ns, c = load_jupyter_config(monkeypatch, tmp_path)
        assert c["ServerApp"]["ip"] == "0.0.0.0"


class TestJupyterAIDefaultPersona:
    """Who answers when the user just types, without picking anyone."""

    def test_opencode_is_the_default(self, monkeypatch, tmp_path):
        _, c = load_jupyter_config(monkeypatch, tmp_path)
        assert c["PersonaManager"]["default_persona_id"] == (
            "jupyter-ai-personas::jupyter_ai_acp_client::OpenCodeAcpPersona"
        )

    def test_id_follows_the_format_base_persona_builds(self, monkeypatch, tmp_path):
        """`BasePersona.id` is
        `jupyter-ai-personas::<package>::<class>`, derived from the module
        path -- not something we get to name. A typo here does not raise: the
        lookup is a plain `.get()`, so the chat just silently answers nobody."""
        _, c = load_jupyter_config(monkeypatch, tmp_path)
        prefix, package, klass = c["PersonaManager"]["default_persona_id"].split("::")
        assert prefix == "jupyter-ai-personas"
        # The distribution that ships the ACP personas, and the class inside it
        # whose module is jupyter_ai_acp_client.acp_personas.opencode.
        assert package == "jupyter_ai_acp_client"
        assert klass == "OpenCodeAcpPersona"

    def test_default_is_the_persona_that_needs_no_account(self, monkeypatch, tmp_path):
        """Claude and Codex refuse until the user logs in with their own
        provider account; OpenCode answers out of the box. Defaulting to either
        of the other two would meet every new user with a login prompt."""
        _, c = load_jupyter_config(monkeypatch, tmp_path)
        assert "OpenCode" in c["PersonaManager"]["default_persona_id"]


class TestJupyterAIMCPServers:
    """What the chat's agents can reach. Registering the AF server replaces
    jupyter-ai's default list, so the notebook toolkit has to be restated or
    personas silently lose the ability to touch the notebook they are in."""

    def _servers(self, monkeypatch, tmp_path, **env):
        _, c = load_jupyter_config(monkeypatch, tmp_path, env=env)
        return {s["name"]: s for s in c["PersonaManager"]["builtin_mcp_servers"]}

    def test_notebook_toolkit_is_kept(self, monkeypatch, tmp_path):
        servers = self._servers(monkeypatch, tmp_path)
        assert "Jupyter MCP Server" in servers

    def test_notebook_toolkit_url_matches_the_port_we_pin(self, monkeypatch, tmp_path):
        _, c = load_jupyter_config(monkeypatch, tmp_path)
        port = c["MCPExtensionApp"]["mcp_port"]
        jupyter = next(
            s
            for s in c["PersonaManager"]["builtin_mcp_servers"]
            if s["name"] == c["MCPExtensionApp"]["mcp_name"]
        )
        assert jupyter["url"] == f"http://localhost:{port}/mcp"

    def test_af_server_registered_with_the_session_token(self, monkeypatch, tmp_path):
        servers = self._servers(
            monkeypatch, tmp_path, JUPYTERHUB_API_TOKEN="tok-123", NAMESPACE="cms-dev"
        )
        af = servers["purdue-af-agentic-interface"]
        assert af["url"] == (
            "http://agentic-interface.cms-dev.svc.cluster.local:8888"
            "/services/agentic-interface/mcp"
        )
        assert af["headers"] == [{"name": "Authorization", "value": "Bearer tok-123"}]

    def test_namespace_defaults_to_cms(self, monkeypatch, tmp_path):
        servers = self._servers(monkeypatch, tmp_path, JUPYTERHUB_API_TOKEN="tok")
        assert (
            "agentic-interface.cms.svc" in servers["purdue-af-agentic-interface"]["url"]
        )

    def test_no_token_means_no_af_server(self, monkeypatch, tmp_path):
        """Half a registration is worse than none: the persona would show the
        server, then fail every call with a 403 the user cannot act on."""
        servers = self._servers(monkeypatch, tmp_path)
        assert "purdue-af-agentic-interface" not in servers

    def test_token_never_reaches_a_file_in_the_persistent_home(
        self, monkeypatch, tmp_path
    ):
        """The token rotates every spawn and the home directory outlives it.
        This registration is built at server start for exactly that reason;
        assert nothing was written down on the way."""
        self._servers(monkeypatch, tmp_path, JUPYTERHUB_API_TOKEN="tok-secret")
        written = [
            path
            for path in tmp_path.rglob("*")
            if path.is_file() and "tok-secret" in path.read_text(errors="ignore")
        ]
        assert written == []


class TestJupyterAIServerNameAgreement:
    """The MCP server has one name across the facility. The skill and the
    platform context name it explicitly, so an agent that meets it under a
    different name in JupyterLab than in the terminal is working from
    instructions that do not describe what it can see."""

    def test_name_and_url_match_config_agents(self, monkeypatch, tmp_path):
        from common import REPO

        hook = (REPO / "docker/purdue-af/scripts/config-agents.sh").read_text()
        _, c = load_jupyter_config(
            monkeypatch, tmp_path, env={"JUPYTERHUB_API_TOKEN": "t"}
        )
        af = next(
            s
            for s in c["PersonaManager"]["builtin_mcp_servers"]
            if s["name"] == "purdue-af-agentic-interface"
        )
        assert 'MCP_NAME="purdue-af-agentic-interface"' in hook
        # Same in-cluster address, with the hook's ${NAMESPACE:-cms} resolved.
        assert af["url"] == (
            "http://agentic-interface.cms.svc.cluster.local:8888"
            "/services/agentic-interface/mcp"
        )
        assert "svc.cluster.local:8888/services/agentic-interface/mcp" in hook
