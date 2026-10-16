"""Tests for tools/elicitation.py + a guard that every elicitation schema is
accepted by the real MCP SDK renderer.

The SDK's render_elicitation_schema rejects any field the spec's
PrimitiveSchemaDefinition does not accept, and it is what goes on the wire;
a fake context cannot check that.
"""

import types

import pytest
from mcp.server.elicitation import render_elicitation_schema
from mcp.shared.exceptions import NoBackChannelError
from tools import dask
from tools.elicitation import elicit, single_choice_model

# Hand-written models used by create_dask_cluster's elicitation flow.
_DASK_ELICIT_MODELS = [
    dask._EnvChoice,
    dask._PixiChoice,
    dask._CondaChoice,
    dask._SizeChoice,
    dask._CustomSize,
    dask._CountChoice,
    dask._CustomCount,
]


@pytest.mark.parametrize("model", _DASK_ELICIT_MODELS)
def test_dask_elicit_models_pass_sdk_validator(model):
    render_elicitation_schema(model)  # raises TypeError if not primitive-only


def test_single_choice_model_passes_sdk_validator_and_uses_titled_oneof():
    model = single_choice_model(
        "Choice",
        ["1", "2", "3"],
        labels=["a", "b", "c"],
        default="2",
        description="pick",
    )
    schema = render_elicitation_schema(model)

    prop = schema["properties"]["value"]
    assert prop["type"] == "string"
    # Standards-compliant titled single-select — inline, no deprecated enumNames.
    assert prop["oneOf"] == [
        {"const": "1", "title": "a"},
        {"const": "2", "title": "b"},
        {"const": "3", "title": "c"},
    ]
    assert "enumNames" not in prop
    assert prop["default"] == "2"
    # No $ref/$defs anywhere (breaks dropdown rendering in some clients).
    assert "$defs" not in schema
    assert "$ref" not in str(schema)


def test_single_choice_model_without_labels_uses_plain_enum():
    model = single_choice_model("Choice", ["red", "blue"])
    prop = render_elicitation_schema(model)["properties"]["value"]
    assert prop["enum"] == ["red", "blue"]
    assert "oneOf" not in prop


def test_single_choice_model_default_falls_back_when_missing():
    model = single_choice_model("Choice", ["a", "b"], default="zzz")
    assert render_elicitation_schema(model)["properties"]["value"]["default"] == "a"


def test_single_choice_model_requires_keys():
    with pytest.raises(ValueError):
        single_choice_model("Choice", [])


# ── elicit diagnostics ────────────────────────────────────────────────────────


def _ctx(client_params, capabilities=None, protocol=None, answer="decline"):
    async def respond(message, schema):
        if isinstance(answer, Exception):
            raise answer
        return types.SimpleNamespace(action=answer)

    return types.SimpleNamespace(
        session=types.SimpleNamespace(client_params=client_params),
        client_capabilities=capabilities,
        protocol_version=protocol,
        elicit=respond,
    )


async def test_elicit_logs_who_answered(caplog):
    """An agent client may auto-decline in milliseconds; the log has to say
    which client it was, on which protocol, and whether it claimed to support
    elicitation."""
    capabilities = types.SimpleNamespace(elicitation=object())
    params = types.SimpleNamespace(
        client_info=types.SimpleNamespace(name="claude-code", version="2.1"),
        capabilities=capabilities,
    )
    with caplog.at_level("INFO", logger="tools.elicitation"):
        assert await elicit(
            _ctx(params, capabilities, "2025-11-25"), "pick", dask._EnvChoice
        ) == ("decline", None)
    assert (
        "client=claude-code/2.1 protocol=2025-11-25 elicitation_capability=True"
        in caplog.text
    )


async def test_elicit_before_initialisation_logs_unknown_client(caplog):
    with caplog.at_level("INFO", logger="tools.elicitation"):
        await elicit(_ctx(None, answer="cancel"), "pick", dask._EnvChoice)
    assert "action=cancel" in caplog.text
    assert "client=None/None protocol=None elicitation_capability=None" in caplog.text


async def test_elicit_without_a_back_channel_is_unsupported_not_an_error(caplog):
    """Stateless HTTP and protocol 2026-07-28 cannot carry a request to the
    client; the tool falls back to asking in chat, so it is not a warning."""
    ctx = _ctx(
        None, protocol="2026-07-28", answer=NoBackChannelError("elicitation/create")
    )
    with caplog.at_level("INFO", logger="tools.elicitation"):
        assert await elicit(ctx, "pick", dask._EnvChoice) == ("unsupported", None)
    assert "no back-channel" in caplog.text
    assert "protocol=2026-07-28" in caplog.text
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_elicit_error_is_unsupported_and_logged(caplog):
    ctx = _ctx(None, answer=RuntimeError("stream closed"))
    with caplog.at_level("INFO", logger="tools.elicitation"):
        assert await elicit(ctx, "pick", dask._EnvChoice) == ("unsupported", None)
    warning = next(r for r in caplog.records if r.levelname == "WARNING")
    assert "stream closed" in warning.getMessage()
