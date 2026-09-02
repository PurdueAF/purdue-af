# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
# mypy: ignore-errors
import logging
import os
import stat
import subprocess

from jupyter_core.paths import jupyter_data_dir

c = get_config()  # noqa: F821
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
    def filter(self, record):
        return "Skipping XSRF check for insecure request" not in record.getMessage()


# XSRF skip lines go to tornado.application, not ServerApp
logging.getLogger("tornado.application").addFilter(_SuppressXSRFSkipNoise())

# to output both image/svg+xml and application/pdf plot formats in the notebook file
c.InlineBackend.figure_formats = {"png", "jpeg", "svg", "pdf"}

# https://github.com/jupyter/notebook/issues/3130
c.FileContentsManager.delete_to_trash = False

# Generate a self-signed certificate
OPENSSL_CONFIG = """\
[req]
distinguished_name = req_distinguished_name
[req_distinguished_name]
"""
if "GEN_CERT" in os.environ:
    dir_name = jupyter_data_dir()
    pem_file = os.path.join(dir_name, "notebook.pem")
    os.makedirs(dir_name, exist_ok=True)

    # Generate an openssl.cnf file to set the distinguished name
    cnf_file = os.path.join(os.getenv("CONDA_DIR", "/usr/lib"), "ssl", "openssl.cnf")
    if not os.path.isfile(cnf_file):
        with open(cnf_file, "w") as fh:
            fh.write(OPENSSL_CONFIG)

    # Generate a certificate if one doesn't exist on disk
    subprocess.check_call(
        [
            "openssl",
            "req",
            "-new",
            "-newkey=rsa:2048",
            "-days=365",
            "-nodes",
            "-x509",
            "-subj=/C=XX/ST=XX/L=XX/O=generated/CN=generated",
            f"-keyout={pem_file}",
            f"-out={pem_file}",
        ]
    )
    # Restrict access to the file
    os.chmod(pem_file, stat.S_IRUSR | stat.S_IWUSR)
    c.ServerApp.certfile = pem_file

# Change default umask for all subprocesses of the notebook server if set in
# the environment
if "NB_UMASK" in os.environ:
    os.umask(int(os.environ["NB_UMASK"], 8))

c.KernelSpecManager.ensure_native_kernel = False


# --------------------------------------------------------------------------
# AI in JupyterLab (jupyter-ai)
# --------------------------------------------------------------------------
# The chat panel and its personas come from the installed stack and need no
# config; what needs config is which MCP servers those personas can reach.
#
# jupyter_server_mcp's own defaults, restated because setting the AF server
# below replaces PersonaManager's default `builtin_mcp_servers` wholesale --
# including the entry that gives personas the notebook toolkit, without which
# they cannot touch the notebook they are sitting in. Pinning both sides here
# is what keeps that entry pointing at the port the extension actually binds.
_JUPYTER_MCP_NAME = "Jupyter MCP Server"
_JUPYTER_MCP_PORT = 3001

# Must match config-agents.sh: same in-cluster address, same server name. The
# skill and the platform context name this server, so an agent reaching it
# under a different name in JupyterLab than in the terminal gets instructions
# that do not match what it sees.
_AF_MCP_NAME = "purdue-af-agentic-interface"
_AF_MCP_URL = (
    "http://agentic-interface.{namespace}.svc.cluster.local:8888"
    "/services/agentic-interface/mcp"
)


def _builtin_mcp_servers():
    """The MCP servers every persona gets, before the user's own
    `.jupyter/mcp_settings.json` is merged on top."""
    servers = [
        {
            "type": "http",
            "name": _JUPYTER_MCP_NAME,
            "url": f"http://localhost:{_JUPYTER_MCP_PORT}/mcp",
            "headers": [],
        }
    ]
    # The token rotates on every spawn. Reading it here, at server start, is the
    # reason this registration lives in the server config and not in the
    # `.jupyter/mcp_settings.json` jupyter-ai also reads: that file takes
    # literal strings, expands nothing, and sits in a persistent home, so a
    # token written into it is stale the moment the session restarts.
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


c.MCPExtensionApp.mcp_name = _JUPYTER_MCP_NAME
c.MCPExtensionApp.mcp_port = _JUPYTER_MCP_PORT
c.PersonaManager.builtin_mcp_servers = _builtin_mcp_servers()


def _patch_websocket_protocol_ping_units():
    """WebSocketMixin uses ms; Tornado protocol expects seconds (see tornado.websocket)."""
    try:
        from tornado.websocket import WebSocketHandler
    except ImportError:
        return
    _orig = WebSocketHandler.get_websocket_protocol

    def _wrapped(self):
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

    WebSocketHandler.get_websocket_protocol = _wrapped


_patch_websocket_protocol_ping_units()
