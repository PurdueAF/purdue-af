"""Request-scoped user context shared between server and tools."""

from contextvars import ContextVar

# Set by _AuthMiddleware for every authenticated request.
# Value: {"username": str, "pod_name": str, "namespace": str, "token": str}
# `token` is the raw JupyterHub Bearer token — tools that call downstream APIs
# (Dask Gateway, JupyterHub server API) read it from here.
current_user: ContextVar[dict] = ContextVar("current_user")
