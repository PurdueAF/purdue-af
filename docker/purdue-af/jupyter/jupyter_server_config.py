# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
import logging
import os
import sys
from typing import Any

c: Any = get_config()  # type: ignore[name-defined]  # noqa: F821
c.ServerApp.ip = "0.0.0.0"
c.ServerApp.open_browser = False
# Quieter default logging
c.ServerApp.log_level = "WARN"
c.ServerApp.tornado_settings = {
    "headers": {
        "Permissions-Policy": "clipboard-read=(self), clipboard-write=(self)",
    },
}

# Avoid double-finish on hidden paths (jupyter-server ContentsHandler.get)
c.FileContentsManager.allow_hidden = True


class _SuppressXSRFSkipNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "Skipping XSRF check for insecure request" not in record.getMessage()


# XSRF skip lines go to tornado.application, not ServerApp
logging.getLogger("tornado.application").addFilter(_SuppressXSRFSkipNoise())

# https://github.com/jupyter/notebook/issues/3130
c.FileContentsManager.delete_to_trash = False

c.KernelSpecManager.ensure_native_kernel = False


# --------------------------------------------------------------------------
# AI in JupyterLab (jupyter-ai)
# --------------------------------------------------------------------------
# Default persona: OpenCode, the only one usable without an account (its Zen
# free models need no key). An id that resolves to nothing means no default.
_DEFAULT_PERSONA_ID = "jupyter-ai-personas::jupyter_ai_acp_client::OpenCodeAcpPersona"

# jupyter_server_mcp's defaults, restated: setting builtin_mcp_servers
# replaces the notebook-toolkit entry too.
_JUPYTER_MCP_NAME = "Jupyter MCP Server"
_JUPYTER_MCP_PORT = 3001

# Must match config-agents.sh: same name, same in-cluster address.
_AF_MCP_NAME = "purdue-af-agentic-interface"
_AF_MCP_URL = (
    "http://agentic-interface.{namespace}.svc.cluster.local:8888"
    "/services/agentic-interface/mcp"
)


def _builtin_mcp_servers() -> list[dict[str, Any]]:
    """The MCP servers every persona gets, before the user's own
    `.jupyter/mcp_settings.json` is merged on top."""
    servers: list[dict[str, Any]] = [
        {
            "type": "http",
            "name": _JUPYTER_MCP_NAME,
            "url": f"http://localhost:{_JUPYTER_MCP_PORT}/mcp",
            "headers": [],
        }
    ]
    # Read at server start, not written to the persistent mcp_settings.json.
    token = os.environ.get("JUPYTERHUB_API_TOKEN")
    if token:
        servers.append(
            {
                "type": "http",
                "name": _AF_MCP_NAME,
                "url": _AF_MCP_URL.format(
                    namespace=os.environ.get("NAMESPACE") or "cms"
                ),
                "headers": [{"name": "Authorization", "value": f"Bearer {token}"}],
            }
        )
    return servers


c.PersonaManager.default_persona_id = _DEFAULT_PERSONA_ID
c.MCPExtensionApp.mcp_name = _JUPYTER_MCP_NAME
c.MCPExtensionApp.mcp_port = _JUPYTER_MCP_PORT
c.PersonaManager.builtin_mcp_servers = _builtin_mcp_servers()


def _patch_websocket_protocol_ping_units() -> None:
    """WebSocketMixin uses ms; Tornado protocol expects seconds (see tornado.websocket)."""
    from tornado.websocket import WebSocketHandler

    _orig = WebSocketHandler.get_websocket_protocol

    def _wrapped(self: WebSocketHandler) -> Any:
        proto = _orig(self)
        if proto is None:
            return None
        p = getattr(proto, "params", None)
        if p is None:
            return proto
        if p.ping_interval is not None and p.ping_interval > 500:
            p.ping_interval /= 1000.0
        if p.ping_timeout is not None and p.ping_timeout > 500:
            p.ping_timeout /= 1000.0
        if (
            p.ping_interval is not None
            and p.ping_timeout is not None
            and p.ping_timeout > p.ping_interval
        ):
            p.ping_timeout = p.ping_interval
        return proto

    setattr(WebSocketHandler, "get_websocket_protocol", _wrapped)


_patch_websocket_protocol_ping_units()


def _start_profiling() -> None:
    server = os.environ.get("PYROSCOPE_SERVER")
    if not server:
        return
    # An exception here would discard this whole file and the session would not answer the hub.
    try:
        import pyroscope

        pyroscope.configure(
            application_name=os.environ["PYROSCOPE_APP"],
            server_address=server,
            sample_rate=100,
            oncpu=True,
            gil_only=True,
            tags={
                "component": "jupyter-server",
                "user": os.environ.get("NB_USER", "unknown"),
            },
        )
    except Exception as exc:
        print(f"Pyroscope profiling not started: {exc!r}", file=sys.stderr)


_start_profiling()
