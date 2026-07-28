"""Validate an uploaded model against Triton's model repository layout.

Triton expects::

    <model-name>/
      [config.pbtxt]
      [<label-file>.txt ...]
      <version>/            # positive integer
        <model-definition>  # name depends on the platform/backend

Errors block the upload; warnings are surfaced to the user but let it through,
because some of Triton's rules depend on server flags we cannot see from here
(notably ``--strict-model-config``).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

# platform -> (expected artifact name, kind)
PLATFORM_ARTIFACTS = {
    "tensorrt_plan": ("model.plan", "file"),
    "onnxruntime_onnx": ("model.onnx", "any"),  # may be a dir for external-data ONNX
    "tensorflow_savedmodel": ("model.savedmodel", "dir"),
    "tensorflow_graphdef": ("model.graphdef", "file"),
    "pytorch_libtorch": ("model.pt", "file"),
    "openvino": ("model.xml", "file"),
    "python": ("model.py", "file"),
    "dali": ("model.dali", "file"),
}

# backend -> expected artifact; None means the backend accepts several layouts
BACKEND_ARTIFACTS = {
    "tensorrt": ("model.plan", "file"),
    "onnxruntime": ("model.onnx", "any"),
    "pytorch": ("model.pt", "file"),
    "openvino": ("model.xml", "file"),
    "python": ("model.py", "file"),
    "dali": ("model.dali", "file"),
    "tensorflow": None,  # savedmodel or graphdef
    "fil": None,         # xgboost.json, checkpoint.tl, ...
    "vllm": None,
}

# Filename -> platform, used when there is no config.pbtxt to read.
ARTIFACT_PLATFORMS = {
    "model.plan": "tensorrt_plan",
    "model.onnx": "onnxruntime_onnx",
    "model.savedmodel": "tensorflow_savedmodel",
    "model.graphdef": "tensorflow_graphdef",
    "model.pt": "pytorch_libtorch",
    "model.xml": "openvino",
    "model.py": "python",
    "model.dali": "dali",
}

# Backends that can infer their config, so config.pbtxt is optional when the
# server runs with --strict-model-config=false.
AUTOCOMPLETE_PLATFORMS = {
    "tensorrt_plan",
    "onnxruntime_onnx",
    "tensorflow_savedmodel",
    "pytorch_libtorch",
    "python",
}


@dataclass
class ValidationResult:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    platform: str = ""
    versions: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "errors": self.errors,
            "warnings": self.warnings,
            "platform": self.platform,
            "versions": self.versions,
        }


def _strip_comments(text: str) -> str:
    return re.sub(r"#[^\n]*", "", text)


def parse_config(config_file: Path) -> dict:
    """Pull the fields we care about out of a config.pbtxt (text protobuf).

    Only top-level scalars are read; nested blocks such as ``input [ {...} ]``
    also contain a ``name`` field, so unindented matches are preferred.
    """
    try:
        text = _strip_comments(config_file.read_text(errors="replace"))
    except OSError as exc:
        return {"_error": str(exc)}

    def scalar(field_name: str, quoted: bool = True):
        pattern = rf'"([^"]*)"' if quoted else r"([0-9]+)"
        # An unindented match is top-level; fall back to the first match anywhere.
        top = re.search(rf"^{field_name}\s*:\s*{pattern}", text, re.MULTILINE)
        if top:
            return top.group(1)
        anywhere = re.search(rf"^\s*{field_name}\s*:\s*{pattern}", text, re.MULTILINE)
        return anywhere.group(1) if anywhere else None

    return {
        "name": scalar("name"),
        "platform": scalar("platform"),
        "backend": scalar("backend"),
        "default_model_filename": scalar("default_model_filename"),
        "max_batch_size": scalar("max_batch_size", quoted=False),
    }


def _expected_artifact(platform: str, backend: str, default_filename: str):
    """(name, kind) of the file Triton will look for inside a version dir."""
    if default_filename:
        return default_filename, "any"
    if platform and platform in PLATFORM_ARTIFACTS:
        return PLATFORM_ARTIFACTS[platform]
    if backend and backend in BACKEND_ARTIFACTS:
        return BACKEND_ARTIFACTS[backend] or (None, None)
    return None, None


def _infer_platform(version_dir: Path) -> str:
    for artifact, platform in ARTIFACT_PLATFORMS.items():
        if (version_dir / artifact).exists():
            return platform
    return ""


def validate_model_dir(model_dir: Path, model_name: str) -> ValidationResult:
    """Check a staged model directory against Triton's expectations."""
    result = ValidationResult()

    if not model_dir.is_dir():
        result.errors.append("The upload does not contain a model directory.")
        return result

    entries = sorted(model_dir.iterdir(), key=lambda p: p.name)
    config_file = model_dir / "config.pbtxt"
    has_config = config_file.is_file()

    config = parse_config(config_file) if has_config else {}
    platform = (config.get("platform") or "").strip()
    backend = (config.get("backend") or "").strip()
    default_filename = (config.get("default_model_filename") or "").strip()

    # -- config.pbtxt sanity ------------------------------------------------
    if has_config:
        if config.get("_error"):
            result.errors.append(f"config.pbtxt could not be read: {config['_error']}")
        declared = (config.get("name") or "").strip()
        if declared and declared != model_name:
            result.errors.append(
                f'config.pbtxt declares name: "{declared}", but the model is being '
                f'installed as "{model_name}". Triton requires these to match.'
            )
        if not platform and not backend:
            result.warnings.append(
                "config.pbtxt sets neither 'platform' nor 'backend'; Triton will have "
                "to infer the backend from the model file."
            )
        if platform == "ensemble" and not (model_dir / "config.pbtxt").is_file():
            result.errors.append("Ensemble models require a config.pbtxt.")

    # -- version directories ------------------------------------------------
    version_dirs = []
    for entry in entries:
        if entry.is_dir():
            if entry.name.isdigit():
                version_dirs.append(entry)
            elif entry.name != "warmup":
                result.warnings.append(
                    f"Directory '{entry.name}/' is not a numeric version and will be "
                    "ignored by Triton."
                )
        elif entry.name != "config.pbtxt" and entry.suffix not in (".txt", ".json"):
            result.warnings.append(
                f"File '{entry.name}' sits outside a version directory and will be "
                "ignored by Triton."
            )

    if not version_dirs:
        # The most common mistake: model files dropped straight into the model dir.
        stray = [e.name for e in entries if e.is_file() and e.name in ARTIFACT_PLATFORMS]
        if stray:
            result.errors.append(
                f"Model file '{stray[0]}' is directly inside the model directory. "
                f"Triton requires a numeric version directory, e.g. 1/{stray[0]}."
            )
        else:
            result.errors.append(
                "No version directory found. A Triton model needs at least one "
                "directory named with a positive integer, e.g. '1/'."
            )
        return result

    for entry in version_dirs:
        if int(entry.name) < 1:
            result.errors.append(
                f"Version directory '{entry.name}/' is invalid: Triton versions start at 1."
            )

    result.versions = sorted((d.name for d in version_dirs), key=int)

    # -- contents of each version -------------------------------------------
    if not platform and not backend and not default_filename:
        platform = _infer_platform(version_dirs[0])

    expected_name, expected_kind = _expected_artifact(platform, backend, default_filename)
    result.platform = platform or backend or ""

    for version_dir in version_dirs:
        contents = list(version_dir.iterdir())
        if platform == "ensemble":
            continue  # ensembles legitimately have empty version directories
        if not contents:
            result.errors.append(f"Version directory '{version_dir.name}/' is empty.")
            continue
        if not expected_name:
            continue  # backend accepts several layouts; leave it to Triton

        target = version_dir / expected_name
        if not target.exists():
            present = ", ".join(sorted(c.name for c in contents)[:6]) or "nothing"
            result.errors.append(
                f"Version '{version_dir.name}/' does not contain '{expected_name}', which "
                f"{result.platform or 'this backend'} requires (found: {present}). "
                "Rename the file, or set default_model_filename in config.pbtxt."
            )
        elif expected_kind == "dir" and not target.is_dir():
            result.errors.append(
                f"'{version_dir.name}/{expected_name}' must be a directory for "
                f"{result.platform}."
            )
        elif expected_kind == "file" and target.is_dir():
            result.errors.append(
                f"'{version_dir.name}/{expected_name}' must be a file for {result.platform}."
            )
        elif expected_name == "model.xml" and not (version_dir / "model.bin").exists():
            result.warnings.append(
                f"Version '{version_dir.name}/' has model.xml but no model.bin; "
                "OpenVINO models normally need both."
            )

    # -- missing config.pbtxt ------------------------------------------------
    if not has_config:
        if platform in AUTOCOMPLETE_PLATFORMS:
            result.warnings.append(
                f"No config.pbtxt. Triton can autocomplete a {platform} model, but only "
                "when the server runs with --strict-model-config=false."
            )
        elif platform:
            result.errors.append(
                f"No config.pbtxt, and {platform} models cannot be autocompleted by "
                "Triton. Add a config.pbtxt."
            )
        else:
            result.errors.append(
                "No config.pbtxt, and the backend could not be inferred from the model "
                "files. Add a config.pbtxt specifying 'platform' or 'backend'."
            )

    return result
