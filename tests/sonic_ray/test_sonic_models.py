"""Finding and running the ONNX models of a Triton-layout directory."""

import numpy as np
import pytest
from sonic_ray.models import InvalidInput, ModelStore, pick_version
from sonic_ray_helpers import doubler_model, reference_particlenet


def test_highest_numbered_version_with_an_onnx_model_wins(make_model):
    model_dir = make_model("m", versions=("1", "3", "10", "notaversion"))
    (model_dir / "10" / "model.onnx").unlink()  # present but empty version dir
    assert pick_version(model_dir).name == "3"
    (model_dir / "3" / "model.onnx").unlink()
    (model_dir / "1" / "model.onnx").unlink()
    assert pick_version(model_dir) is None


def test_store_loads_onnx_and_explains_the_rest(store):
    assert sorted(store.models) == ["doubler", "particleNetFromMiniAODAK8"]
    assert store.skipped == {"deepmet": "no version directory holds model.onnx"}
    assert store.models["particleNetFromMiniAODAK8"].version == "1"


def test_unloadable_models_are_reported_not_fatal(make_model, repo):
    make_model("corrupt")  # b"not a model"
    make_model("empty", versions=())
    store = ModelStore(repo, providers=("CPUExecutionProvider",))
    assert store.models == {}
    assert store.skipped["corrupt"].startswith("failed to load")
    assert store.skipped["empty"].startswith("no version directory")


def test_unavailable_providers_fail_loudly(make_model, repo):
    make_model("m", model=doubler_model())
    store = ModelStore(repo, providers=("NoSuchExecutionProvider",))
    assert "none of the execution providers" in store.skipped["m"]


def test_missing_root_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        ModelStore(tmp_path / "nowhere")


def test_metadata_comes_from_the_model_itself(store):
    meta = store.get("particleNetFromMiniAODAK8").metadata()
    assert meta["name"] == "particleNetFromMiniAODAK8" and meta["version"] == "1"
    assert meta["inputs"] == [
        {"name": "pf_features", "dtype": "float32", "shape": [-1, 4, -1]},
        {"name": "pf_mask", "dtype": "float32", "shape": [-1, 1, -1]},
    ]
    assert meta["outputs"] == [{"name": "output", "dtype": "float32", "shape": [-1, 4]}]
    assert meta["providers"] == ["CPUExecutionProvider"]
    assert store.get("doubler").metadata()["inputs"] == [
        {"name": "x", "dtype": "int32", "shape": [3]}
    ]
    with pytest.raises(KeyError):
        store.get("deepmet")


def test_infer_runs_the_model_on_a_dynamic_particle_count(store):
    model = store.get("particleNetFromMiniAODAK8")
    rng = np.random.default_rng(0)
    for particles in (7, 100):
        features = rng.normal(size=(3, 4, particles)).astype(np.float32)
        mask = (rng.random((3, 1, particles)) > 0.3).astype(np.float32)
        # what a JSON client sends: nested lists, not arrays
        out = model.infer({"pf_features": features.tolist(), "pf_mask": mask.tolist()})
        assert set(out) == {"output"}
        np.testing.assert_allclose(
            out["output"], reference_particlenet(features, mask), rtol=1e-5, atol=1e-5
        )


def test_inputs_are_cast_to_the_models_dtype(store):
    out = store.get("doubler").infer({"x": [1, 2, 3]})  # ints from JSON → int32
    np.testing.assert_array_equal(out["y"], [2, 4, 6])
    assert out["y"].dtype == np.int32


def test_infer_validates_before_touching_the_runtime(store):
    model = store.get("particleNetFromMiniAODAK8")
    ok = {
        "pf_features": np.zeros((2, 4, 5)).tolist(),
        "pf_mask": np.zeros((2, 1, 5)).tolist(),
    }
    with pytest.raises(InvalidInput, match="unexpected input"):
        model.infer({**ok, "extra": [1.0]})
    with pytest.raises(InvalidInput, match="missing input"):
        model.infer({"pf_features": ok["pf_features"]})
    with pytest.raises(InvalidInput, match="rank 1, model wants 3"):
        model.infer({**ok, "pf_mask": [1.0, 2.0]})
    with pytest.raises(InvalidInput, match="input 'pf_mask'"):
        model.infer({**ok, "pf_mask": [[["not a number"]]]})
    # a shape only ORT can reject surfaces as a 400, not a stack trace
    with pytest.raises(InvalidInput, match="inference failed"):
        model.infer({**ok, "pf_mask": np.zeros((2, 2, 5)).tolist()})
