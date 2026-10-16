"""Shared MCP elicitation helpers.

Elicitation lets a tool pause mid-call and ask the client for structured input;
capable clients render the schema as a form (enum fields become dropdowns /
radio buttons). These helpers keep the request/fallback handling and dynamic
schema construction in one place so individual tools stay small.
"""

import logging
import time
from typing import Any, NamedTuple, cast

from mcp.shared.exceptions import NoBackChannelError
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)


class _Client(NamedTuple):
    name: str | None
    version: str | None
    elicitation: bool | None
    protocol: str | None

    def __str__(self) -> str:
        return (
            f"client={self.name}/{self.version} protocol={self.protocol} "
            f"elicitation_capability={self.elicitation}"
        )


def _client_diag(ctx: Any) -> _Client:
    """Who is asking, as far as the connection says; None for anything unknown."""
    try:
        params = ctx.session.client_params
        info = getattr(params, "client_info", None)
        caps = ctx.client_capabilities
        return _Client(
            getattr(info, "name", None),
            getattr(info, "version", None),
            None if caps is None else getattr(caps, "elicitation", None) is not None,
            ctx.protocol_version,
        )
    except Exception:
        return _Client(None, None, None, None)


async def elicit(ctx: Any, message: str, schema: type[BaseModel]) -> tuple[str, Any]:
    """Ask the client for structured input.

    Returns ``(status, data)`` where status is one of:
      • 'accept'      — user submitted the form; data is a ``schema`` instance
      • 'decline' / 'cancel' — client rejected/dismissed the prompt; data is None
      • 'unsupported' — no context, a connection that cannot carry a request
        to the client (stateless HTTP, protocol 2026-07-28 and later), or the
        request errored; data is None

    Note: an agent client (per the MCP spec) may auto-generate a response to an
    elicitation instead of showing a form — surfacing as a near-instant
    'decline'/'cancel'. The diagnostics logged here (client identity, protocol
    version, declared elicitation capability, action, round-trip time) make
    that visible.
    """
    if ctx is None:
        return "unsupported", None

    client = _client_diag(ctx)
    start = time.monotonic()
    try:
        result = await ctx.elicit(message=message, schema=schema)
    except NoBackChannelError:
        logger.info("elicit unavailable: no back-channel to the client, %s", client)
        return "unsupported", None
    except Exception as exc:
        logger.warning("elicit error: %s error=%r", client, exc)
        return "unsupported", None

    elapsed_ms = (time.monotonic() - start) * 1000.0
    action = getattr(result, "action", "unsupported")
    logger.info("elicit action=%s in %.0fms %s", action, elapsed_ms, client)
    if action == "accept":
        return "accept", getattr(result, "data", None)
    return action, None


class NeedsChoices(Exception):
    """A choice could not be collected interactively.

    Carries the text the tool hands back instead of a result: everything the
    agent needs to ask the user in chat and call again with explicit
    arguments. Raised by ``ask`` and turned into an ordinary (non-error)
    result by the server, so no tool has to check elicitation statuses.
    """


async def ask(ctx: Any, message: str, schema: type[BaseModel], help_text: str) -> Any:
    """``elicit`` that returns the submitted data or raises NeedsChoices."""
    status, data = await elicit(ctx, message, schema)
    if status != "accept":
        raise NeedsChoices(help_text)
    return data


def single_choice_model(
    model_name: str,
    keys: list[str],
    *,
    labels: list[str] | None = None,
    default: str | None = None,
    description: str = "",
    field: str = "value",
) -> type[BaseModel]:
    """Build a one-field elicitation model whose value is one of ``keys``.

    Emits the standards-compliant, inline single-select enum shape:
      • with ``labels`` → a titled enum: ``oneOf: [{const, title}, …]``
      • without labels → a plain ``enum: [...]``
    No deprecated ``enumNames`` and no ``$ref``/``$defs`` (which break dropdown
    rendering): the field is a flat ``str`` whose choices ride in
    ``json_schema_extra``, so the whole schema stays inline. The chosen value
    is read from ``<instance>.value`` (or ``field``).
    """
    if not keys:
        raise ValueError("single_choice_model requires at least one key")

    default_val = default if default in keys else keys[0]
    if labels:
        extra: dict = {
            "oneOf": [{"const": k, "title": lbl} for k, lbl in zip(keys, labels)]
        }
    else:
        extra = {"enum": list(keys)}
    field_info = Field(
        default=default_val,
        description=description,
        json_schema_extra=extra,
    )
    fields: dict[str, Any] = {field: (str, field_info)}
    return cast(type[BaseModel], create_model(model_name, **fields))
