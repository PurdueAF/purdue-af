"""ONNX models from a Triton-layout directory, run with ONNX Runtime.

The directory is the one the SuperSONIC model manager writes::

    /models/
      particleNetFromMiniAODAK8/1/model.onnx
      deepmet/3/model.graphdef          # not ONNX: skipped, with the reason

Every subdirectory is a model; its highest numbered version directory holding
``model.onnx`` is what gets loaded (Triton's default version policy). Nothing
else in the layout — ``config.pbtxt`` included — is read: ONNX Runtime knows
the model's inputs and outputs itself.

Nothing here knows about Ray or HTTP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)

MODEL_FILENAME = "model.onnx"
DEFAULT_PROVIDERS = ("CUDAExecutionProvider", "CPUExecutionProvider")

# ONNX Runtime's tensor type strings → numpy dtypes.
ORT_DTYPES: dict[str, type[np.generic]] = {
    "tensor(bool)": np.bool_,
    "tensor(uint8)": np.uint8,
    "tensor(uint16)": np.uint16,
    "tensor(uint32)": np.uint32,
    "tensor(uint64)": np.uint64,
    "tensor(int8)": np.int8,
    "tensor(int16)": np.int16,
    "tensor(int32)": np.int32,
    "tensor(int64)": np.int64,
    "tensor(float16)": np.float16,
    "tensor(float)": np.float32,
    "tensor(double)": np.float64,
}


class InvalidInput(ValueError):
    """The request does not fit the model (HTTP 400)."""


@dataclass
class Tensor:
    name: str
    dtype: type[np.generic]
    # -1 for a dynamic dimension (ONNX Runtime reports those as strings).
    shape: list[int]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dtype": np.dtype(self.dtype).name,
            "shape": self.shape,
        }


def pick_version(model_dir: Path) -> Path | None:
    """The highest numbered version directory holding model.onnx."""
    versions = [
        p
        for p in model_dir.iterdir()
        if p.is_dir() and p.name.isdigit() and (p / MODEL_FILENAME).is_file()
    ]
    return max(versions, key=lambda p: int(p.name)) if versions else None


class Model:
    """One loaded ONNX model: its tensors and a way to run it."""

    def __init__(self, name: str, version_dir: Path, providers: tuple[str, ...]):
        import onnxruntime as ort

        self.name = name
        self.version = version_dir.name
        self.path = version_dir / MODEL_FILENAME
        available = set(ort.get_available_providers())
        chosen = [p for p in providers if p in available]
        if not chosen:
            raise RuntimeError(
                f"none of the execution providers {list(providers)} is available; "
                f"onnxruntime offers {sorted(available)}"
            )
        self.session = ort.InferenceSession(str(self.path), providers=chosen)
        self.providers: list[str] = list(self.session.get_providers())
        self.inputs = [_tensor(n) for n in self.session.get_inputs()]
        self.outputs = [_tensor(n) for n in self.session.get_outputs()]

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "inputs": [t.describe() for t in self.inputs],
            "outputs": [t.describe() for t in self.outputs],
            "providers": self.providers,
        }

    def infer(self, inputs: dict[str, Any]) -> dict[str, np.ndarray]:
        """Run the model on named arrays (anything numpy can turn into one).
        Inputs are cast to the model's dtypes; names and ranks must match."""
        expected = {t.name: t for t in self.inputs}
        unknown = sorted(set(inputs) - set(expected))
        if unknown:
            raise InvalidInput(
                f"unexpected input(s) {unknown}; model takes {sorted(expected)}"
            )
        missing = sorted(set(expected) - set(inputs))
        if missing:
            raise InvalidInput(f"missing input(s) {missing}")
        feed: dict[str, np.ndarray] = {}
        for name, value in inputs.items():
            spec = expected[name]
            try:
                array = np.asarray(value, dtype=spec.dtype)
            except (TypeError, ValueError) as exc:
                raise InvalidInput(f"input {name!r}: {exc}") from None
            if array.ndim != len(spec.shape):
                raise InvalidInput(
                    f"input {name!r} has rank {array.ndim}, model wants {len(spec.shape)} "
                    f"(shape {spec.shape})"
                )
            feed[name] = array
        try:
            results = self.session.run(None, feed)
        except Exception as exc:  # ORT's own hierarchy; the shapes were wrong
            raise InvalidInput(f"inference failed: {exc}") from exc
        return {t.name: np.asarray(r) for t, r in zip(self.outputs, results)}


def _tensor(node: Any) -> Tensor:
    return Tensor(
        name=node.name,
        dtype=ORT_DTYPES.get(node.type, np.float32),
        shape=[d if isinstance(d, int) else -1 for d in node.shape],
    )


class ModelStore:
    """Every ONNX model under ``root``, loaded once. Directories that hold no
    ONNX model, or whose model fails to load, are kept in ``skipped`` with
    the reason, so a client listing models sees why one is missing."""

    def __init__(
        self, root: Path | str, providers: tuple[str, ...] = DEFAULT_PROVIDERS
    ):
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"model repository {self.root} is not a directory")
        self.models: dict[str, Model] = {}
        self.skipped: dict[str, str] = {}
        for model_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            name = model_dir.name
            version_dir = pick_version(model_dir)
            if version_dir is None:
                self.skipped[name] = f"no version directory holds {MODEL_FILENAME}"
            else:
                try:
                    self.models[name] = Model(name, version_dir, providers)
                except Exception as exc:  # anything ORT throws while loading
                    self.skipped[name] = f"failed to load: {exc}"
            if name in self.models:
                LOGGER.info(
                    "model %s: loaded version %s", name, self.models[name].version
                )
            else:
                LOGGER.info("model %s: skipped (%s)", name, self.skipped[name])

    def get(self, name: str) -> Model:
        try:
            return self.models[name]
        except KeyError:
            raise KeyError(name) from None
