from contextvars import ContextVar
from typing import Optional, TypedDict


class User(TypedDict):
    """The authenticated caller of a request."""

    username: str
    token: str


# Set by the auth middleware before tool handlers run.
current_user: ContextVar[Optional[User]] = ContextVar("current_user", default=None)


def require_user() -> User:
    """The authenticated user for the current request.

    An unset value means a handler was reached outside the authenticated path:
    a wiring bug, not a user error.
    """
    user = current_user.get()
    if user is None:
        raise RuntimeError(
            "no authenticated user in context — tool invoked outside the "
            "authenticated request path"
        )
    return user
