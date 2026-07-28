"""Triton model repository layout rules applied to uploads."""

from model_manager.validation import parse_config, validate_model_dir


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --------------------------------------------------------------------------
# Layouts that must be accepted
# --------------------------------------------------------------------------


def test_accepts_standard_onnx_model(staged, make_model):
    model = make_model("particlenet", parent=staged)

    result = validate_model_dir(model, "particlenet")

    assert result.ok, result.errors
    assert result.warnings == []
    assert result.platform == "onnxruntime_onnx"
    assert result.versions == ["1"]


def test_accepts_multiple_versions_sorted_numerically(staged, make_model):
    model = make_model("deepmet", versions=("1", "2", "10"), parent=staged)

    result = validate_model_dir(model, "deepmet")

    assert result.ok, result.errors
    assert result.versions == ["1", "2", "10"]


def test_accepts_savedmodel_directory(staged):
    model = staged / "tf_model"
    write(
        model / "config.pbtxt", 'name: "tf_model"\nplatform: "tensorflow_savedmodel"\n'
    )
    write(model / "1" / "model.savedmodel" / "saved_model.pb", "x")

    result = validate_model_dir(model, "tf_model")

    assert result.ok, result.errors


def test_accepts_ensemble_with_empty_version_dir(staged):
    model = staged / "ens"
    write(model / "config.pbtxt", 'name: "ens"\nplatform: "ensemble"\n')
    (model / "1").mkdir()

    result = validate_model_dir(model, "ens")

    assert result.ok, result.errors


def test_accepts_default_model_filename_override(staged):
    model = staged / "custom"
    write(
        model / "config.pbtxt",
        'name: "custom"\nbackend: "onnxruntime"\ndefault_model_filename: "net.onnx"\n',
    )
    write(model / "1" / "net.onnx", "x")

    result = validate_model_dir(model, "custom")

    assert result.ok, result.errors


def test_label_and_preprocess_files_are_not_flagged(staged, make_model):
    """CMS models on CVMFS ship these next to config.pbtxt."""
    model = make_model("higgsInteractionNet", parent=staged)
    write(model / "higgs_interactionnet_labels.txt", "a\nb\n")
    write(model / "preprocess.json", "{}")

    result = validate_model_dir(model, "higgsInteractionNet")

    assert result.ok, result.errors
    assert result.warnings == []


# --------------------------------------------------------------------------
# Layouts that must be rejected
# --------------------------------------------------------------------------


def test_rejects_model_file_outside_version_directory(staged):
    model = staged / "flat"
    write(model / "config.pbtxt", 'name: "flat"\nplatform: "onnxruntime_onnx"\n')
    write(model / "model.onnx", "x")

    result = validate_model_dir(model, "flat")

    assert not result.ok
    assert "1/model.onnx" in " ".join(result.errors)


def test_rejects_missing_version_directory(staged):
    model = staged / "empty"
    write(model / "config.pbtxt", 'name: "empty"\nplatform: "onnxruntime_onnx"\n')

    result = validate_model_dir(model, "empty")

    assert not result.ok
    assert any("version directory" in e for e in result.errors)


def test_rejects_empty_version_directory(staged):
    model = staged / "hollow"
    write(model / "config.pbtxt", 'name: "hollow"\nplatform: "pytorch_libtorch"\n')
    (model / "1").mkdir()

    result = validate_model_dir(model, "hollow")

    assert not result.ok
    assert any("is empty" in e for e in result.errors)


def test_rejects_version_zero(staged, make_model):
    model = make_model("zero", versions=("0",), parent=staged)

    result = validate_model_dir(model, "zero")

    assert not result.ok
    assert any("start at 1" in e for e in result.errors)


def test_rejects_config_name_mismatch(staged):
    """Triton refuses to load a model whose config name is not its directory."""
    model = staged / "installed_as"
    write(
        model / "config.pbtxt", 'name: "something_else"\nplatform: "onnxruntime_onnx"\n'
    )
    write(model / "1" / "model.onnx", "x")

    result = validate_model_dir(model, "installed_as")

    assert not result.ok
    assert any("something_else" in e and "installed_as" in e for e in result.errors)


def test_rejects_wrong_artifact_name_for_platform(staged):
    model = staged / "misnamed"
    write(model / "config.pbtxt", 'name: "misnamed"\nplatform: "onnxruntime_onnx"\n')
    write(model / "1" / "mymodel.onnx", "x")

    result = validate_model_dir(model, "misnamed")

    assert not result.ok
    assert any("model.onnx" in e for e in result.errors)


def test_rejects_savedmodel_as_file(staged):
    model = staged / "tf_flat"
    write(
        model / "config.pbtxt", 'name: "tf_flat"\nplatform: "tensorflow_savedmodel"\n'
    )
    write(model / "1" / "model.savedmodel", "not a directory")

    result = validate_model_dir(model, "tf_flat")

    assert not result.ok
    assert any("must be a directory" in e for e in result.errors)


def test_rejects_missing_config_for_non_autocompletable_backend(staged):
    model = staged / "gd"
    write(model / "1" / "model.graphdef", "x")

    result = validate_model_dir(model, "gd")

    assert not result.ok
    assert any("cannot be autocompleted" in e for e in result.errors)


def test_rejects_unidentifiable_model(staged):
    model = staged / "mystery"
    write(model / "1" / "weights.bin", "x")

    result = validate_model_dir(model, "mystery")

    assert not result.ok
    assert any("could not be inferred" in e for e in result.errors)


# --------------------------------------------------------------------------
# Warnings (accepted, but surfaced)
# --------------------------------------------------------------------------


def test_warns_when_config_missing_but_autocompletable(staged):
    model = staged / "auto"
    write(model / "1" / "model.onnx", "x")

    result = validate_model_dir(model, "auto")

    assert result.ok, result.errors
    assert any("strict-model-config" in w for w in result.warnings)


def test_warns_about_ignored_files_and_directories(staged, make_model):
    model = make_model("stray", parent=staged)
    write(model / "notes.md", "hi")
    (model / "scratch").mkdir()

    result = validate_model_dir(model, "stray")

    assert result.ok, result.errors
    joined = " ".join(result.warnings)
    assert "notes.md" in joined
    assert "scratch" in joined


def test_warns_when_openvino_bin_missing(staged):
    model = staged / "ov"
    write(model / "config.pbtxt", 'name: "ov"\nplatform: "openvino"\n')
    write(model / "1" / "model.xml", "x")

    result = validate_model_dir(model, "ov")

    assert result.ok, result.errors
    assert any("model.bin" in w for w in result.warnings)


# --------------------------------------------------------------------------
# config.pbtxt parsing
# --------------------------------------------------------------------------


def test_parse_config_prefers_top_level_name_over_nested_input_names(tmp_path):
    """`input [{ name: ... }]` blocks also contain a name field."""
    config = write(
        tmp_path / "config.pbtxt",
        'name: "outer"\n'
        'platform: "onnxruntime_onnx"\n'
        "max_batch_size: 500\n"
        "input [{\n"
        '  name: "input_cpf"\n'
        "  data_type: TYPE_FP32\n"
        "}]\n",
    )

    parsed = parse_config(config)

    assert parsed["name"] == "outer"
    assert parsed["platform"] == "onnxruntime_onnx"
    assert parsed["max_batch_size"] == "500"


def test_parse_config_ignores_comments(tmp_path):
    config = write(
        tmp_path / "config.pbtxt",
        '# name: "commented_out"\nname: "real"\nbackend: "python"\n',
    )

    parsed = parse_config(config)

    assert parsed["name"] == "real"
    assert parsed["backend"] == "python"


def test_missing_directory_is_an_error(tmp_path):
    result = validate_model_dir(tmp_path / "nope", "nope")

    assert not result.ok
