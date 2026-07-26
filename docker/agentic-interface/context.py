from contextvars import ContextVar
from typing import Any, Optional

# Per-request user context, set by the auth middleware before tool handlers run.
# Value: {"username": str, "namespace": str, "token": str}
current_user: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "current_user", default=None
)


def require_user() -> dict[str, Any]:
    """The authenticated user for the current request.

    The ASGI auth middleware sets `current_user` before any tool can run, so
    an unset value means a handler was reached outside the authenticated
    path — a wiring bug, not a user error. Raising here turns what would be
    an opaque `NoneType is not subscriptable` deep inside a tool into a
    named failure, and lets every call site treat the user as a plain dict.
    """
    user = current_user.get()
    if user is None:
        raise RuntimeError(
            "no authenticated user in context — tool invoked outside the "
            "authenticated request path"
        )
    return user
