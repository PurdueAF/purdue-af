"""Triton model repository layout rules applied to uploads."""

import pytest
from model_manager import validation
from model_manager.validation import parse_config, validate_model_dir


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def config(platform):
    return f'name: "m"\nplatform: "{platform}"\n'


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


@pytest.mark.parametrize(
    "files,error",
    [
        pytest.param(
            {"config.pbtxt": config("onnxruntime_onnx"), "model.onnx": "x"},
            "1/model.onnx",
            id="model-file-outside-version-directory",
        ),
        pytest.param(
            {"config.pbtxt": config("onnxruntime_onnx")},
            "No version directory",
            id="missing-version-directory",
        ),
        pytest.param(
            {"config.pbtxt": config("pytorch_libtorch"), "1": None},
            "'1/' is empty",
            id="empty-version-directory",
        ),
        pytest.param(
            {"config.pbtxt": config("onnxruntime_onnx"), "0/model.onnx": "x"},
            "start at 1",
            id="version-zero",
        ),
        pytest.param(
            {
                "config.pbtxt": 'name: "other"\nplatform: "onnxruntime_onnx"\n',
                "1/model.onnx": "x",
            },
            'declares name: "other", but the model is being installed as "m"',
            id="config-name-is-not-the-directory",
        ),
        pytest.param(
            {"config.pbtxt": config("onnxruntime_onnx"), "1/mymodel.onnx": "x"},
            "does not contain 'model.onnx'",
            id="wrong-artifact-name-for-platform",
        ),
        pytest.param(
            {
                "config.pbtxt": config("tensorflow_savedmodel"),
                "1/model.savedmodel": "not a directory",
            },
            "must be a directory",
            id="savedmodel-as-a-file",
        ),
        pytest.param(
            {"1/model.graphdef": "x"},
            "cannot be autocompleted",
            id="no-config-for-a-non-autocompletable-backend",
        ),
        pytest.param(
            {"1/weights.bin": "x"},
            "could not be inferred",
            id="unidentifiable-model",
        ),
        pytest.param(
            {
                "config.pbtxt": config("pytorch_libtorch"),
                "1/model.pt/data.pkl": "x",
            },
            "must be a file",
            id="torchscript-model-as-a-directory",
        ),
    ],
)
def test_rejects_invalid_layout(staged, files, error):
    model = staged / "m"
    for path, text in files.items():
        if text is None:
            (model / path).mkdir(parents=True)
        else:
            write(model / path, text)

    result = validate_model_dir(model, "m")

    assert not result.ok
    assert any(error in e for e in result.errors), result.errors


def test_unreadable_config_is_an_error(staged, make_model, monkeypatch):
    model = make_model("locked", parent=staged)
    monkeypatch.setattr(validation, "parse_config", lambda _: {"_error": "denied"})

    result = validate_model_dir(model, "locked")

    assert not result.ok
    assert any("could not be read: denied" in e for e in result.errors)


def test_unicode_digit_directory_is_not_a_version(staged, make_model):
    """'²'.isdigit() is True but int('²') raises."""
    model = make_model("sup", parent=staged)
    (model / "²").mkdir()

    result = validate_model_dir(model, "sup")

    assert result.ok, result.errors
    assert result.versions == ["1"]
    assert any("'²/'" in w for w in result.warnings)


def test_warns_when_config_missing_but_autocompletable(staged):
    model = staged / "auto"
    write(model / "1" / "model.onnx", "x")

    result = validate_model_dir(model, "auto")

    assert result.ok, result.errors
    assert any("disable-auto-complete-config" in w for w in result.warnings)


def test_warns_about_ignored_files_and_directories(staged, make_model):
    model = make_model("stray", parent=staged)
    write(model / "notes.md", "hi")
    (model / "scratch").mkdir()

    result = validate_model_dir(model, "stray")

    assert result.ok, result.errors
    joined = " ".join(result.warnings)
    assert "notes.md" in joined
    assert "scratch" in joined


def test_multi_layout_backend_is_left_to_triton(staged):
    model = staged / "tf"
    write(model / "config.pbtxt", 'name: "tf"\nbackend: "tensorflow"\n')
    write(model / "1" / "anything.bin", "x")

    result = validate_model_dir(model, "tf")

    assert result.ok, result.errors
    assert result.platform == "tensorflow"


def test_warns_when_config_names_no_backend(staged):
    model = staged / "bare"
    write(model / "config.pbtxt", 'name: "bare"\n')
    write(model / "1" / "model.onnx", "x")

    result = validate_model_dir(model, "bare")

    assert result.ok, result.errors
    assert any("neither 'platform' nor 'backend'" in w for w in result.warnings)


def test_warns_when_openvino_bin_missing(staged):
    model = staged / "ov"
    write(model / "config.pbtxt", 'name: "ov"\nplatform: "openvino"\n')
    write(model / "1" / "model.xml", "x")

    result = validate_model_dir(model, "ov")

    assert result.ok, result.errors
    assert any("model.bin" in w for w in result.warnings)


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


def test_parse_config_ignores_comments(tmp_path):
    config = write(
        tmp_path / "config.pbtxt",
        '# name: "commented_out"\nname: "real"\nbackend: "python"\n',
    )

    parsed = parse_config(config)

    assert parsed["name"] == "real"
    assert parsed["backend"] == "python"


def test_parse_config_reports_read_errors(tmp_path):
    assert "_error" in parse_config(tmp_path / "absent.pbtxt")


def test_missing_directory_is_an_error(tmp_path):
    result = validate_model_dir(tmp_path / "nope", "nope")

    assert not result.ok
