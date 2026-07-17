from contextvars import ContextVar
from typing import Optional

# Per-request user context, set by the auth middleware before tool handlers run.
# Value: {"username": str, "namespace": str, "token": str}
current_user: ContextVar[Optional[dict]] = ContextVar("current_user", default=None)
