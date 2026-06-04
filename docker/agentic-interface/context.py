"""Request-scoped user context shared between server and tools."""

from contextvars import ContextVar

# Set by _AuthMiddleware for every authenticated request.
# Value: {"username": str, "pod_name": str, "namespace": str}
current_user: ContextVar[dict] = ContextVar("current_user")
