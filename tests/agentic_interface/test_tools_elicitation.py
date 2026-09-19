"""Tests for tools/elicitation.py + a guard that every elicitation schema is
accepted by the real MCP SDK validator.

The SDK's _validate_elicitation_schema accepts only raw primitive field
annotations (str/int/float/bool); a fake context cannot check that.
"""

import types

import pytest
from mcp.server.elicitation import _validate_elicitation_schema
from tools import dask
from tools.elicitation import elicit, single_choice_model

# Hand-written models used by create_dask_cluster's elicitation flow.
_DASK_ELICIT_MODELS = [
    dask._BackendChoice,
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
    _validate_elicitation_schema(model)  # raises TypeError if not primitive-only


def test_single_choice_model_passes_sdk_validator_and_uses_titled_oneof():
    model = single_choice_model(
        "Choice",
        ["1", "2", "3"],
        labels=["a", "b", "c"],
        default="2",
        description="pick",
    )
    _validate_elicitation_schema(model)

    schema = model.model_json_schema()
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
    model = single_choice_model("Choice", ["k8s", "slurm"])
    _validate_elicitation_schema(model)
    prop = model.model_json_schema()["properties"]["value"]
    assert prop["enum"] == ["k8s", "slurm"]
    assert "oneOf" not in prop


def test_single_choice_model_default_falls_back_when_missing():
    model = single_choice_model("Choice", ["a", "b"], default="zzz")
    assert model.model_json_schema()["properties"]["value"]["default"] == "a"


def test_single_choice_model_requires_keys():
    with pytest.raises(ValueError):
        single_choice_model("Choice", [])


# ── elicit diagnostics ────────────────────────────────────────────────────────


def _ctx(client_params, action="decline"):
    async def respond(message, schema):
        return types.SimpleNamespace(action=action)

    return types.SimpleNamespace(
        session=types.SimpleNamespace(client_params=client_params), elicit=respond
    )


async def test_elicit_logs_who_answered(caplog):
    """An agent client may auto-decline in milliseconds; the log has to say
    which client it was and whether it claimed to support elicitation."""
    params = types.SimpleNamespace(
        clientInfo=types.SimpleNamespace(name="claude-code", version="2.1"),
        capabilities=types.SimpleNamespace(elicitation=object()),
    )
    with caplog.at_level("INFO", logger="tools.elicitation"):
        assert await elicit(_ctx(params), "pick", dask._BackendChoice) == (
            "decline",
            None,
        )
    assert "client=claude-code/2.1 elicitation_capability=True" in caplog.text


async def test_elicit_before_initialisation_logs_unknown_client(caplog):
    with caplog.at_level("INFO", logger="tools.elicitation"):
        await elicit(_ctx(None, action="cancel"), "pick", dask._BackendChoice)
    assert "action=cancel" in caplog.text
    assert "client=None/None elicitation_capability=None" in caplog.text
