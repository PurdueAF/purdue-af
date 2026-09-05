"""A Triton-layout model repository, run with ONNX Runtime.

The repository is the one the SuperSONIC model manager writes and the
supersonic release's Triton reads::

    /models/
      particleNetFromMiniAODAK8/
        config.pbtxt            # protobuf text format: platform, I/O, batching
        1/model.onnx
      deepmet/
        config.pbtxt            # platform: "tensorflow_graphdef"
        3/model.graphdef

Every directory with a ``config.pbtxt`` is a model. The ones whose platform
is ``onnxruntime_onnx`` (or backend ``onnxruntime``) are loaded; the rest are
listed as UNAVAILABLE with the reason, so a client asking ``/v2/repository/
index`` sees exactly which of Triton's models this server does not serve
rather than a 404 that could mean anything.

Nothing here knows about Ray or HTTP.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from sonic_ray.protocol import numpy_dtype_name

LOGGER = logging.getLogger(__name__)

CONFIG_FILENAME = "config.pbtxt"
DEFAULT_MODEL_FILENAME = "model.onnx"
SUPPORTED_PLATFORMS = {"onnxruntime_onnx"}
SUPPORTED_BACKENDS = {"onnxruntime"}
DEFAULT_PROVIDERS = ("CUDAExecutionProvider", "CPUExecutionProvider")

# Triton's TYPE_* names → v2 datatype names (protocol.DTYPES keys).
TRITON_TO_V2 = {
    "TYPE_BOOL": "BOOL",
    "TYPE_UINT8": "UINT8",
    "TYPE_UINT16": "UINT16",
    "TYPE_UINT32": "UINT32",
    "TYPE_UINT64": "UINT64",
    "TYPE_INT8": "INT8",
    "TYPE_INT16": "INT16",
    "TYPE_INT32": "INT32",
    "TYPE_INT64": "INT64",
    "TYPE_FP16": "FP16",
    "TYPE_FP32": "FP32",
    "TYPE_FP64": "FP64",
    "TYPE_STRING": "BYTES",
}
# ONNX Runtime's tensor type strings → v2 datatype names.
ORT_TO_V2 = {
    "tensor(bool)": "BOOL",
    "tensor(uint8)": "UINT8",
    "tensor(uint16)": "UINT16",
    "tensor(uint32)": "UINT32",
    "tensor(uint64)": "UINT64",
    "tensor(int8)": "INT8",
    "tensor(int16)": "INT16",
    "tensor(int32)": "INT32",
    "tensor(int64)": "INT64",
    "tensor(float16)": "FP16",
    "tensor(float)": "FP32",
    "tensor(double)": "FP64",
    "tensor(string)": "BYTES",
}


class ModelError(Exception):
    """Base for everything a caller can be blamed for or told about."""


class UnknownModel(ModelError, KeyError):
    """No such model in the repository (HTTP 404)."""


class ModelUnavailable(ModelError):
    """The model exists but is not loaded; ``reason`` says why (HTTP 503)."""

    def __init__(self, name: str, reason: str):
        super().__init__(f"model {name!r} is not available: {reason}")
        self.reason = reason


class InvalidInput(ModelError, ValueError):
    """The request does not fit the model (HTTP 400)."""


# --------------------------------------------------------------------------
# config.pbtxt
# --------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""
    \s+                          # whitespace
  | \#[^\n]*                     # comment
  | "(?:[^"\\]|\\.)*"            # double-quoted string
  | '(?:[^'\\]|\\.)*'            # single-quoted string
  | [{}\[\],:]                   # punctuation
  | [^\s{}\[\],:"'#]+            # bare token: key, number, enum, bool
    """,
    re.VERBOSE,
)


def parse_pbtxt(text: str) -> dict[str, Any]:
    """Parse protobuf text format into dicts and lists.

    Covers what Triton model configs use: ``key: value``, ``key { ... }``,
    ``key [ a, b ]``, ``key [ { ... }, { ... } ]``, ``#`` comments, quoted
    strings, numbers, enums and bools. A key that repeats becomes a list, so
    ``input { } input { }`` and ``input [ { }, { } ]`` read the same.
    """
    tokens = [t for t in _TOKEN.findall(text) if t.strip() and not t.startswith("#")]
    if "".join(_TOKEN.findall(text)) != text:
        raise ValueError("config.pbtxt has characters the parser does not understand")
    parser = _Parser(tokens)
    message = parser.message(top_level=True)
    if parser.pos != len(tokens):
        raise ValueError(f"unexpected token {tokens[parser.pos]!r} in config.pbtxt")
    return message


class _Parser:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> str:
        if self.pos >= len(self.tokens):
            raise ValueError("config.pbtxt ends unexpectedly")
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, token: str) -> None:
        got = self.take()
        if got != token:
            raise ValueError(f"expected {token!r}, got {got!r} in config.pbtxt")

    def message(self, top_level: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {}
        repeated: set[str] = set()
        while True:
            token = self.peek()
            if token is None:
                if top_level:
                    return result
                raise ValueError("unterminated message in config.pbtxt")
            if token == "}":
                if top_level:
                    raise ValueError("unmatched '}' in config.pbtxt")
                return result
            if token == ",":  # tolerated between fields
                self.take()
                continue
            key = self.take()
            if self.peek() == ":":
                self.take()
            value = self.value()
            if key in result:
                if key not in repeated:
                    result[key] = [result[key]]
                    repeated.add(key)
                result[key].append(value)
            else:
                result[key] = value

    def value(self) -> Any:
        token = self.take()
        if token == "{":
            message = self.message()
            self.expect("}")
            return message
        if token == "[":
            items: list[Any] = []
            while self.peek() != "]":
                items.append(self.value())
                if self.peek() == ",":
                    self.take()
            self.expect("]")
            return items
        return _scalar(token)


def _scalar(token: str) -> Any:
    if token[0] in "\"'":
        return token[1:-1].encode().decode("unicode_escape")
    if token in ("true", "True"):
        return True
    if token in ("false", "False"):
        return False
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token  # enum such as TYPE_FP32


@dataclass
class TensorSpec:
    name: str
    datatype: str
    shape: list[int]

    def as_v2(self) -> dict[str, Any]:
        return {"name": self.name, "datatype": self.datatype, "shape": self.shape}


@dataclass
class ModelConfig:
    """The parts of config.pbtxt this server acts on."""

    name: str
    platform: str
    backend: str
    max_batch_size: int
    inputs: list[TensorSpec]
    outputs: list[TensorSpec]
    model_filename: str

    @classmethod
    def from_text(cls, text: str, directory_name: str) -> ModelConfig:
        raw = parse_pbtxt(text)
        return cls(
            name=str(raw.get("name") or directory_name),
            platform=str(raw.get("platform", "")),
            backend=str(raw.get("backend", "")),
            max_batch_size=int(raw.get("max_batch_size", 0)),
            inputs=[_tensor_spec(t) for t in _as_list(raw.get("input"))],
            outputs=[_tensor_spec(t) for t in _as_list(raw.get("output"))],
            model_filename=str(
                raw.get("default_model_filename") or DEFAULT_MODEL_FILENAME
            ),
        )

    @property
    def supported(self) -> bool:
        return (
            self.platform in SUPPORTED_PLATFORMS or self.backend in SUPPORTED_BACKENDS
        )

    @property
    def batched(self) -> bool:
        """Triton semantics: ``max_batch_size > 0`` means the config's dims
        omit a leading batch dimension that every request carries."""
        return self.max_batch_size > 0


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _tensor_spec(raw: dict[str, Any]) -> TensorSpec:
    dims = _as_list(raw.get("dims"))
    return TensorSpec(
        name=str(raw["name"]),
        datatype=TRITON_TO_V2.get(
            str(raw.get("data_type", "")), str(raw.get("data_type", ""))
        ),
        shape=[int(d) for d in dims],
    )


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


def pick_version(model_dir: Path, model_filename: str | None) -> Path | None:
    """The highest numeric version directory holding the model file — what
    Triton's default version policy (``latest: {num_versions: 1}``) loads.
    With no filename, any non-empty version directory counts: that is how a
    model this server does not run is still reported at Triton's version."""

    def holds_model(version_dir: Path) -> bool:
        if model_filename is None:
            return any(f.is_file() for f in version_dir.iterdir())
        return (version_dir / model_filename).is_file()

    candidates = [
        p
        for p in model_dir.iterdir()
        if p.is_dir() and p.name.isdigit() and holds_model(p)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: int(p.name))


@dataclass
class Model:
    """One loaded ONNX model: its metadata and a way to run it."""

    config: ModelConfig
    version: str
    path: Path
    session: Any  # onnxruntime.InferenceSession; typed loosely to keep import lazy
    inputs: list[TensorSpec]
    outputs: list[TensorSpec]
    providers: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.config.name

    @classmethod
    def load(
        cls, config: ModelConfig, version_dir: Path, providers: tuple[str, ...]
    ) -> Model:
        import onnxruntime as ort

        path = version_dir / config.model_filename
        options = ort.SessionOptions()
        # ORT's own graph optimizations; Triton runs the same backend with
        # the same default, so a model tuned there behaves the same here.
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        available = set(ort.get_available_providers())
        chosen = [p for p in providers if p in available]
        if not chosen:
            raise RuntimeError(
                f"none of the requested execution providers {list(providers)} "
                f"is available; onnxruntime offers {sorted(available)}"
            )
        session = ort.InferenceSession(
            str(path), sess_options=options, providers=chosen
        )
        inputs = [_ort_spec(i) for i in session.get_inputs()]
        outputs = [_ort_spec(o) for o in session.get_outputs()]
        return cls(
            config=config,
            version=version_dir.name,
            path=path,
            session=session,
            inputs=inputs,
            outputs=outputs,
            providers=list(session.get_providers()),
        )

    def metadata(self) -> dict[str, Any]:
        """The v2 model-metadata document, shaped as Triton shapes it: the
        tensor specs come from the config (batch dimension omitted when the
        model is batched), since that is what the client was written against."""
        return {
            "name": self.name,
            "versions": [self.version],
            "platform": self.config.platform or self.config.backend,
            "inputs": [t.as_v2() for t in (self.config.inputs or self.inputs)],
            "outputs": [t.as_v2() for t in (self.config.outputs or self.outputs)],
        }

    def infer(self, inputs: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Run the model. Validates names, dtypes and the batch size the way
        Triton would, so a client gets a 400 with a reason instead of an ORT
        stack trace."""
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
        batch_sizes = set()
        for name, array in inputs.items():
            spec = expected[name]
            if (
                spec.datatype not in ("BYTES", "")
                and _v2_of(array.dtype) != spec.datatype
            ):
                raise InvalidInput(
                    f"input {name!r} is {_v2_of(array.dtype)}, model wants {spec.datatype}"
                )
            if self.config.batched:
                if array.ndim == 0:
                    raise InvalidInput(f"input {name!r} has no batch dimension")
                batch_sizes.add(int(array.shape[0]))
            feed[name] = np.ascontiguousarray(array)
        if self.config.batched:
            if len(batch_sizes) > 1:
                raise InvalidInput(
                    f"inputs disagree on the batch size: {sorted(batch_sizes)}"
                )
            batch = batch_sizes.pop() if batch_sizes else 0
            if batch > self.config.max_batch_size:
                raise InvalidInput(
                    f"batch size {batch} exceeds the model's max_batch_size "
                    f"{self.config.max_batch_size}"
                )
        try:
            results = self.session.run(None, feed)
        except Exception as exc:  # ORT raises its own hierarchy; surface as 400
            raise InvalidInput(f"inference failed: {exc}") from exc
        return {spec.name: np.asarray(r) for spec, r in zip(self.outputs, results)}


def _ort_spec(node: Any) -> TensorSpec:
    return TensorSpec(
        name=node.name,
        datatype=ORT_TO_V2.get(node.type, node.type),
        # ORT reports symbolic dims as strings ('N', 'unk__12'); v2 says -1.
        shape=[d if isinstance(d, int) else -1 for d in node.shape],
    )


def _v2_of(dtype: np.dtype) -> str:
    return numpy_dtype_name(dtype)


# --------------------------------------------------------------------------
# the repository
# --------------------------------------------------------------------------


@dataclass
class ModelEntry:
    """One directory of the repository, loaded or not."""

    name: str
    version: str
    state: str  # READY | UNAVAILABLE
    reason: str = ""
    model: Model | None = None

    def as_index_entry(self) -> dict[str, Any]:
        entry = {"name": self.name, "version": self.version, "state": self.state}
        if self.reason:
            entry["reason"] = self.reason
        return entry


class ModelRepository:
    """Every model under ``root``, loaded once at construction."""

    def __init__(
        self,
        root: Path | str,
        providers: tuple[str, ...] = DEFAULT_PROVIDERS,
        only: set[str] | None = None,
    ):
        self.root = Path(root)
        self.providers = providers
        self.entries: dict[str, ModelEntry] = {}
        if not self.root.is_dir():
            raise FileNotFoundError(f"model repository {self.root} is not a directory")
        for model_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            if only is not None and model_dir.name not in only:
                continue
            entry = self._load(model_dir)
            self.entries[entry.name] = entry
            LOGGER.info(
                "model %s version %s: %s%s",
                entry.name,
                entry.version or "-",
                entry.state,
                f" ({entry.reason})" if entry.reason else "",
            )

    def _load(self, model_dir: Path) -> ModelEntry:
        name = model_dir.name
        config_path = model_dir / CONFIG_FILENAME
        if not config_path.is_file():
            return ModelEntry(name, "", "UNAVAILABLE", f"no {CONFIG_FILENAME}")
        try:
            config = ModelConfig.from_text(config_path.read_text(), name)
        except (ValueError, KeyError) as exc:
            return ModelEntry(
                name, "", "UNAVAILABLE", f"unreadable {CONFIG_FILENAME}: {exc}"
            )
        if not config.supported:
            version_dir = pick_version(model_dir, None)
            version = version_dir.name if version_dir else ""
            what = config.platform or config.backend or "unspecified"
            return ModelEntry(
                config.name,
                version,
                "UNAVAILABLE",
                f"platform {what!r} is not served here; only "
                f"{sorted(SUPPORTED_PLATFORMS)} models are",
            )
        version_dir = pick_version(model_dir, config.model_filename)
        version = version_dir.name if version_dir else ""
        if version_dir is None:
            return ModelEntry(
                config.name,
                "",
                "UNAVAILABLE",
                f"no version directory holds {config.model_filename}",
            )
        try:
            model = Model.load(config, version_dir, self.providers)
        except Exception as exc:  # anything ORT throws while loading
            return ModelEntry(
                config.name, version, "UNAVAILABLE", f"failed to load: {exc}"
            )
        return ModelEntry(config.name, version, "READY", model=model)

    # -- queries -----------------------------------------------------------

    def index(self) -> list[dict[str, Any]]:
        return [e.as_index_entry() for e in self.entries.values()]

    def ready(self) -> list[str]:
        return [e.name for e in self.entries.values() if e.state == "READY"]

    def get(self, name: str, version: str | None = None) -> Model:
        entry = self.entries.get(name)
        if entry is None:
            raise UnknownModel(name)
        if entry.model is None:
            raise ModelUnavailable(name, entry.reason)
        if version is not None and version != entry.version:
            raise UnknownModel(f"{name} version {version}")
        return entry.model
