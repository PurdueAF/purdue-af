"""Shared MCP elicitation helpers.

Elicitation lets a tool pause mid-call and ask the client for structured input;
capable clients render the schema as a form (enum fields become dropdowns /
radio buttons). These helpers keep the request/fallback handling and dynamic
schema construction in one place so individual tools stay small.
"""

from pydantic import BaseModel, Field, create_model


async def elicit(ctx, message: str, schema: type[BaseModel]):
    """Ask the client for structured input.

    Returns ``(status, data)`` where status is one of:
      • 'accept'      — user submitted the form; data is a ``schema`` instance
      • 'decline' / 'cancel' — user rejected the prompt; data is None
      • 'unsupported' — no context, or the client cannot elicit; data is None
    """
    if ctx is None:
        return "unsupported", None
    try:
        result = await ctx.elicit(message=message, schema=schema)
    except Exception:
        return "unsupported", None
    action = getattr(result, "action", "unsupported")
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

    The field is a plain ``str`` carrying ``enum`` (the machine keys) and, when
    ``labels`` is given, ``enumNames`` (human-readable, same order) in its JSON
    schema — the shape MCP elicitation clients render as a dropdown/radio.

    A ``str`` annotation (rather than ``Literal``) is required: the MCP SDK's
    elicitation schema validator only accepts raw primitive annotations and
    rejects ``Literal``. The chosen value is read from ``<instance>.value`` (or
    the name given by ``field``).
    """
    if not keys:
        raise ValueError("single_choice_model requires at least one key")

    default_val = default if default in keys else keys[0]
    extra: dict = {"enum": list(keys)}
    if labels:
        extra["enumNames"] = list(labels)
    field_info = Field(
        default=default_val,
        description=description,
        json_schema_extra=extra,
    )
    return create_model(model_name, **{field: (str, field_info)})
