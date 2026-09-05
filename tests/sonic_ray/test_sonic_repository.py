"""The Triton-layout repository: config parsing, what loads, what does not,
and inference that validates like Triton would."""

import numpy as np
import pytest
from sonic_ray.repository import (
    InvalidInput,
    ModelConfig,
    ModelRepository,
    ModelUnavailable,
    UnknownModel,
    parse_pbtxt,
    pick_version,
)
from sonic_ray_helpers import (
    DEEPMET_CONFIG,
    PARTICLENET_CONFIG,
    doubler_model,
    particlenet_like_model,
    reference_particlenet,
)

# -- config.pbtxt -------------------------------------------------------------


def test_parses_the_real_particlenet_config():
    raw = parse_pbtxt(PARTICLENET_CONFIG)
    assert raw["name"] == "particleNetFromMiniAODAK8"
    assert raw["platform"] == "onnxruntime_onnx"
    assert raw["max_batch_size"] == 500  # `key : value` with a space
    assert raw["dynamic_batching"] == {"preferred_batch_size": [200]}
    assert [i["name"] for i in raw["input"]] == ["pf_features", "pf_mask"]
    assert raw["input"][0]["dims"] == [4, -1]
    assert raw["input"][0]["data_type"] == "TYPE_FP32"  # enum stays a bare token
    assert raw["output"][0]["label_filename"] == "particlenet_labels.txt"
    assert raw["optimization"] == {"graph": {"level": -1}}


def test_repeated_message_fields_and_list_syntax_read_the_same():
    braces = parse_pbtxt('input { name: "a" } input { name: "b" }')
    brackets = parse_pbtxt('input [ { name: "a" }, { name: "b" } ]')
    assert braces == brackets == {"input": [{"name": "a"}, {"name": "b"}]}
    # a single message is not wrapped in a list; ModelConfig normalises that
    assert parse_pbtxt('input { name: "a" }') == {"input": {"name": "a"}}


def test_parser_handles_comments_strings_bools_and_floats():
    raw = parse_pbtxt(
        """
        # a comment
        name: "quoted # not a comment"   # trailing comment
        flag: true
        other: false
        ratio: 0.5
        nested { deep { value: -3 } }
        """
    )
    assert raw == {
        "name": "quoted # not a comment",
        "flag": True,
        "other": False,
        "ratio": 0.5,
        "nested": {"deep": {"value": -3}},
    }


@pytest.mark.parametrize(
    "text", ["name: {", "input [ { name: 'a' ", "} ", 'x: "unterminated']
)
def test_parser_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_pbtxt(text)


def test_model_config_reads_what_the_server_acts_on():
    config = ModelConfig.from_text(PARTICLENET_CONFIG, "dirname")
    assert config.name == "particleNetFromMiniAODAK8"
    assert config.supported and config.batched
    assert config.max_batch_size == 500
    assert [(t.name, t.datatype, t.shape) for t in config.inputs] == [
        ("pf_features", "FP32", [4, -1]),
        ("pf_mask", "FP32", [1, -1]),
    ]
    assert config.outputs[0].datatype == "FP32"
    assert config.model_filename == "model.onnx"

    tf = ModelConfig.from_text(DEEPMET_CONFIG, "deepmet")
    assert not tf.supported

    # name falls back to the directory, filename to default_model_filename
    bare = ModelConfig.from_text(
        'backend: "onnxruntime"\ndefault_model_filename: "net.onnx"', "d"
    )
    assert bare.name == "d" and bare.supported and bare.model_filename == "net.onnx"
    assert not bare.batched


# -- loading ------------------------------------------------------------------


def test_highest_numeric_version_with_the_artifact_wins(make_model, repo):
    model_dir = make_model("m", None, versions=("1", "3", "10", "notaversion"))
    (model_dir / "10" / "model.onnx").unlink()  # present but empty version dir
    assert pick_version(model_dir, "model.onnx").name == "3"
    assert pick_version(model_dir, "other.onnx") is None
    # no filename: any non-empty version directory (the artifact of a model
    # this server does not run still says which version Triton would load)
    assert pick_version(model_dir, None).name == "3"


def test_repository_loads_onnx_and_lists_the_rest(repository):
    index = {e["name"]: e for e in repository.index()}
    assert index["particleNetFromMiniAODAK8"] == {
        "name": "particleNetFromMiniAODAK8",
        "version": "1",
        "state": "READY",
    }
    assert index["doubler"]["state"] == "READY"
    assert index["deepmet"]["state"] == "UNAVAILABLE"
    assert index["deepmet"]["version"] == "3"  # Triton's default policy: latest
    assert "tensorflow_graphdef" in index["deepmet"]["reason"]
    assert repository.ready() == ["doubler", "particleNetFromMiniAODAK8"]


def test_unreadable_configs_and_missing_artifacts_are_reported_not_fatal(
    make_model, repo
):
    make_model("noconfig", None)
    make_model("badconfig", "name: {{{")
    make_model("noartifact", 'platform: "onnxruntime_onnx"', versions=())
    make_model("corrupt", 'platform: "onnxruntime_onnx"')  # b"not a model"
    repository = ModelRepository(repo, providers=("CPUExecutionProvider",))
    reasons = {e["name"]: e["reason"] for e in repository.index()}
    assert reasons["noconfig"] == "no config.pbtxt"
    assert reasons["badconfig"].startswith("unreadable config.pbtxt")
    assert "no version directory" in reasons["noartifact"]
    assert reasons["corrupt"].startswith("failed to load")
    assert repository.ready() == []


def test_allowlist_restricts_what_is_loaded(populated, repo):
    repository = ModelRepository(
        repo, providers=("CPUExecutionProvider",), only={"doubler"}
    )
    assert list(repository.entries) == ["doubler"]


def test_unavailable_providers_fail_loudly(make_model, repo):
    make_model("m", 'platform: "onnxruntime_onnx"', model=doubler_model())
    repository = ModelRepository(repo, providers=("NoSuchExecutionProvider",))
    assert (
        "none of the requested execution providers" in repository.index()[0]["reason"]
    )


def test_missing_root_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        ModelRepository(tmp_path / "nowhere")


# -- lookup -------------------------------------------------------------------


def test_get_distinguishes_unknown_unavailable_and_wrong_version(repository):
    model = repository.get("particleNetFromMiniAODAK8")
    assert model.version == "1"
    assert repository.get("particleNetFromMiniAODAK8", "1") is model
    with pytest.raises(UnknownModel):
        repository.get("particleNetFromMiniAODAK8", "2")
    with pytest.raises(UnknownModel):
        repository.get("nope")
    with pytest.raises(ModelUnavailable) as exc:
        repository.get("deepmet")
    assert "tensorflow_graphdef" in exc.value.reason


def test_metadata_is_shaped_like_tritons(repository):
    """Tensor specs come from config.pbtxt — batch dimension omitted — because
    that is the contract the clients were written against."""
    meta = repository.get("particleNetFromMiniAODAK8").metadata()
    assert meta["name"] == "particleNetFromMiniAODAK8"
    assert meta["versions"] == ["1"]
    assert meta["platform"] == "onnxruntime_onnx"
    assert meta["inputs"] == [
        {"name": "pf_features", "datatype": "FP32", "shape": [4, -1]},
        {"name": "pf_mask", "datatype": "FP32", "shape": [1, -1]},
    ]
    assert meta["outputs"] == [{"name": "output", "datatype": "FP32", "shape": [4]}]

    # without I/O in the config, ORT's own view is used (-1 for symbolic dims)
    bare = repository.get("doubler").metadata()
    assert bare["inputs"] == [{"name": "x", "datatype": "INT32", "shape": [3]}]


# -- inference ----------------------------------------------------------------


def test_infer_runs_the_model_on_a_dynamic_particle_count(repository):
    model = repository.get("particleNetFromMiniAODAK8")
    rng = np.random.default_rng(0)
    for particles in (7, 100):
        features = rng.normal(size=(3, 4, particles)).astype(np.float32)
        mask = (rng.random((3, 1, particles)) > 0.3).astype(np.float32)
        out = model.infer({"pf_features": features, "pf_mask": mask})
        assert set(out) == {"output"}
        np.testing.assert_allclose(
            out["output"], reference_particlenet(features, mask), rtol=1e-5, atol=1e-5
        )


def test_infer_validates_like_triton(repository):
    model = repository.get("particleNetFromMiniAODAK8")
    ok = {
        "pf_features": np.zeros((2, 4, 5), np.float32),
        "pf_mask": np.zeros((2, 1, 5), np.float32),
    }
    with pytest.raises(InvalidInput, match="unexpected input"):
        model.infer({**ok, "extra": np.zeros(1, np.float32)})
    with pytest.raises(InvalidInput, match="missing input"):
        model.infer({"pf_features": ok["pf_features"]})
    with pytest.raises(InvalidInput, match="model wants FP32"):
        model.infer({**ok, "pf_mask": ok["pf_mask"].astype(np.float64)})
    with pytest.raises(InvalidInput, match="disagree on the batch size"):
        model.infer({**ok, "pf_mask": np.zeros((3, 1, 5), np.float32)})
    with pytest.raises(InvalidInput, match="exceeds the model's max_batch_size"):
        model.infer(
            {
                "pf_features": np.zeros((501, 4, 5), np.float32),
                "pf_mask": np.zeros((501, 1, 5), np.float32),
            }
        )
    # a shape ORT itself rejects surfaces as a 400, not a stack trace
    with pytest.raises(InvalidInput, match="inference failed"):
        model.infer({**ok, "pf_mask": np.zeros((2, 2, 5), np.float32)})


def test_unbatched_model_skips_batch_checks(repository):
    model = repository.get("doubler")
    out = model.infer({"x": np.array([1, 2, 3], np.int32)})
    np.testing.assert_array_equal(out["y"], [2, 4, 6])
    assert out["y"].dtype == np.int32


def test_particlenet_like_fixture_matches_its_reference():
    """Guard the fixture itself: if the tiny model drifts from the numpy
    reference, every inference test above would be asserting nonsense."""
    import onnxruntime as ort

    session = ort.InferenceSession(
        particlenet_like_model().SerializeToString(), providers=["CPUExecutionProvider"]
    )
    features = np.ones((1, 4, 3), np.float32)
    mask = np.array([[[1, 0, 1]]], np.float32)
    (out,) = session.run(None, {"pf_features": features, "pf_mask": mask})
    np.testing.assert_array_equal(out, [[2, 2, 2, 2]])
