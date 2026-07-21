"""Shared MCP elicitation helpers.

Elicitation lets a tool pause mid-call and ask the client for structured input;
capable clients render the schema as a form (enum fields become dropdowns /
radio buttons). These helpers keep the request/fallback handling and dynamic
schema construction in one place so individual tools stay small.
"""

import logging
import time

from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)


def _client_diag(ctx) -> tuple[str | None, str | None, bool | None]:
    """(client_name, client_version, declared_elicitation_capability) or Nones."""
    try:
        params = ctx.session.client_params
        if params is None:
            return None, None, None
        info = getattr(params, "clientInfo", None)
        caps = getattr(params, "capabilities", None)
        return (
            getattr(info, "name", None),
            getattr(info, "version", None),
            getattr(caps, "elicitation", None) is not None,
        )
    except Exception:
        return None, None, None


async def elicit(ctx, message: str, schema: type[BaseModel]):
    """Ask the client for structured input.

    Returns ``(status, data)`` where status is one of:
      • 'accept'      — user submitted the form; data is a ``schema`` instance
      • 'decline' / 'cancel' — client rejected/dismissed the prompt; data is None
      • 'unsupported' — no context, or the request errored; data is None

    Note: an agent client (per the MCP spec) may auto-generate a response to an
    elicitation instead of showing a form — surfacing as a near-instant
    'decline'/'cancel'. The diagnostics logged here (client identity, declared
    elicitation capability, action, round-trip time) make that visible.
    """
    if ctx is None:
        return "unsupported", None

    name, version, cap = _client_diag(ctx)
    start = time.monotonic()
    try:
        result = await ctx.elicit(message=message, schema=schema)
    except Exception as exc:
        logger.warning(
            "elicit error: client=%s/%s elicitation_capability=%s error=%r",
            name,
            version,
            cap,
            exc,
        )
        return "unsupported", None

    elapsed_ms = (time.monotonic() - start) * 1000.0
    action = getattr(result, "action", "unsupported")
    logger.info(
        "elicit action=%s in %.0fms client=%s/%s elicitation_capability=%s",
        action,
        elapsed_ms,
        name,
        version,
        cap,
    )
    if action == "accept":
        return "accept", getattr(result, "data", None)
    return action, None


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
    We deliberately avoid the deprecated ``enumNames`` (which newer clients no
    longer render) and any ``$ref``/``$defs`` (which break dropdown rendering) —
    the field is a flat ``str`` so the whole schema stays inline.

    A ``str`` annotation (rather than ``Literal``) is also required: the MCP SDK's
    elicitation validator only accepts raw primitive annotations and rejects
    ``Literal``. The chosen value is read from ``<instance>.value`` (or ``field``).
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
    return create_model(model_name, **{field: (str, field_info)})
