"""Ray Serve inference server over a Triton-layout model repository.

Mounted into the Ray pods from a ConfigMap and imported by Ray Serve as
``sonic_serve:app`` — see apps/ray/sonic-ray/rayservice.yaml.

It speaks the KServe v2 REST protocol (the Open Inference Protocol), the same
protocol Triton serves in the SuperSONIC release, over the endpoints a client
needs to discover models and run inference::

    GET  /v2                       server metadata
    GET  /v2/health/live
    GET  /v2/health/ready
    GET  /v2/repository/index      what the repository holds
    GET  /v2/models/{name}         model metadata (inputs/outputs)
    GET  /v2/models/{name}/ready
    POST /v2/models/{name}/infer   inference

The repository layout is Triton's (``<repo>/<model>/<version>/model.onnx``,
``config.pbtxt`` optional), but the backend is ONNX Runtime, not Triton: models
in a format ORT cannot open — TensorRT plans, SavedModels, TorchScript — are
reported UNAVAILABLE with a reason instead of served. Tensors are exchanged as
JSON; Triton's binary tensor extension is not implemented.

Models load on first request and stay loaded, so a replica warms up as traffic
reaches it rather than paying for the whole repository at startup.
"""

from __future__ import annotations

import asyncio
import ctypes
import glob
import logging
import os
import site
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import Body, FastAPI, HTTPException
from ray import serve

logger = logging.getLogger("ray.serve")

SERVER_NAME = "sonic-ray"
SERVER_VERSION = "2"
SERVER_EXTENSIONS: list[str] = []

# KServe v2 datatype names <-> numpy. BYTES is deliberately absent: it needs
# the binary extension to be useful.
DTYPE_TO_NUMPY: dict[str, Any] = {
    "BOOL": np.bool_,
    "UINT8": np.uint8,
    "UINT16": np.uint16,
    "UINT32": np.uint32,
    "UINT64": np.uint64,
    "INT8": np.int8,
    "INT16": np.int16,
    "INT32": np.int32,
    "INT64": np.int64,
    "FP16": np.float16,
    "FP32": np.float32,
    "FP64": np.float64,
}
NUMPY_TO_DTYPE = {np.dtype(v): k for k, v in DTYPE_TO_NUMPY.items()}

# ONNX Runtime's own type strings.
ONNX_TO_DTYPE = {
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


def _preload_nvidia_libs() -> None:
    """Make the CUDA/cuDNN shared objects from the ``nvidia-*`` wheels visible.

    ONNX Runtime dlopen()s libcudnn and friends by soname. The Ray image
    carries CUDA but not necessarily the cuDNN major ORT was built against, so
    the runtime_env installs the nvidia wheels — which land in site-packages,
    somewhere no loader searches. LD_LIBRARY_PATH cannot help: the path only
    exists once the runtime env has been built, long after the process started.
    Loading them RTLD_GLOBAL here puts them in the process's symbol table
    before ORT looks.
    """
    roots: list[str] = []
    for source in (site.getsitepackages, site.getusersitepackages):
        try:
            found = source()
        except AttributeError:  # not every interpreter layout defines both
            continue
        roots.extend([found] if isinstance(found, str) else found)
    loaded = 0
    for root in roots:
        for pattern in ("nvidia/*/lib/lib*.so", "nvidia/*/lib/lib*.so.*"):
            for lib in sorted(glob.glob(os.path.join(root, pattern))):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                    loaded += 1
                except OSError:  # a stub, or a lib whose own deps are absent
                    continue
    logger.info("preloaded %d NVIDIA shared objects", loaded)


def _import_onnxruntime():
    import onnxruntime as ort

    # ORT >= 1.21 knows how to find the nvidia wheels itself.
    preload = getattr(ort, "preload_dlls", None)
    if callable(preload):
        try:
            preload()
        except Exception:  # noqa: BLE001 - never fail startup over a warm-up
            logger.warning("onnxruntime.preload_dlls() failed", exc_info=True)
    else:
        _preload_nvidia_libs()
    return ort


def _model_file(version_dir: Path) -> Path | None:
    """The ONNX file Triton would pick for this version directory."""
    default = version_dir / "model.onnx"
    if default.exists():
        # An ONNX model with external data is a *directory* named model.onnx
        # holding the graph plus its weight files.
        if default.is_dir():
            inner = sorted(default.glob("*.onnx"))
            return inner[0] if inner else None
        return default
    candidates = sorted(version_dir.glob("*.onnx"))
    return candidates[0] if len(candidates) == 1 else None


def _describe(model_dir: Path) -> dict[str, Any]:
    """State of one model directory, in the shape /v2/repository/index wants."""
    versions = sorted(
        (p for p in model_dir.iterdir() if p.is_dir() and p.name.isdigit()),
        key=lambda p: int(p.name),
    )
    if not versions:
        return {
            "name": model_dir.name,
            "state": "UNAVAILABLE",
            "reason": "no version directory",
        }

    version = versions[-1]
    path = _model_file(version)
    if path is None:
        return {
            "name": model_dir.name,
            "version": version.name,
            "state": "UNAVAILABLE",
            # The honest failure mode: this is a Triton repository and only its
            # ONNX models can be served here.
            "reason": "no ONNX model in the version directory (ONNX Runtime is the only backend)",
        }
    return {
        "name": model_dir.name,
        "version": version.name,
        "state": "READY",
        "path": str(path),
    }


api = FastAPI(title=f"{SERVER_NAME} inference server", docs_url=None, redoc_url=None)


@serve.deployment
@serve.ingress(api)
class InferenceServer:
    """One replica per GPU, mirroring one Triton pod per GPU."""

    def __init__(self, model_repository: str = "/models", session_threads: int = 8):
        self._repository = Path(model_repository)
        self._sessions: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=session_threads, thread_name_prefix="onnx"
        )
        self._ort = _import_onnxruntime()

        available = self._ort.get_available_providers()
        if "CUDAExecutionProvider" in available:
            self._providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        else:
            # Still a working server, just a slow one — and the reason is in
            # /v2 rather than buried in a replica log.
            logger.warning(
                "CUDAExecutionProvider unavailable (have: %s); serving on CPU",
                available,
            )
            self._providers = ["CPUExecutionProvider"]

        logger.info("serving %s with providers %s", self._repository, self._providers)

    # -- repository ---------------------------------------------------------

    def _index(self) -> list[dict[str, Any]]:
        if not self._repository.is_dir():
            return []
        return [
            _describe(d)
            for d in sorted(self._repository.iterdir())
            if d.is_dir() and not d.name.startswith(".")
        ]

    def _session(self, name: str):
        """Load on first use, then keep. Loading is serialised; inference is not."""
        session = self._sessions.get(name)
        if session is not None:
            return session

        with self._lock:
            session = self._sessions.get(name)
            if session is not None:
                return session

            model_dir = self._repository / name
            if not model_dir.is_dir():
                raise HTTPException(
                    status_code=404, detail=f"Request for unknown model: '{name}'"
                )
            entry = _describe(model_dir)
            if entry["state"] != "READY":
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{name}' is not available: {entry['reason']}",
                )

            options = self._ort.SessionOptions()
            options.graph_optimization_level = (
                self._ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            )
            try:
                session = self._ort.InferenceSession(
                    entry["path"], sess_options=options, providers=self._providers
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller
                logger.exception("failed to load %s", name)
                raise HTTPException(
                    status_code=400, detail=f"Model '{name}' failed to load: {exc}"
                ) from exc

            self._sessions[name] = session
            logger.info("loaded %s (%s)", name, session.get_providers()[0])
            return session

    @staticmethod
    def _metadata(name: str, version: str, session) -> dict[str, Any]:
        def spec(node) -> dict[str, Any]:
            return {
                "name": node.name,
                "datatype": ONNX_TO_DTYPE.get(node.type, node.type),
                # ONNX names dynamic axes; KServe v2 spells them -1.
                "shape": [d if isinstance(d, int) else -1 for d in node.shape],
            }

        return {
            "name": name,
            "versions": [version],
            "platform": "onnxruntime_onnx",
            "inputs": [spec(i) for i in session.get_inputs()],
            "outputs": [spec(o) for o in session.get_outputs()],
        }

    # -- KServe v2 ----------------------------------------------------------

    @api.get("/v2")
    def server_metadata(self) -> dict[str, Any]:
        return {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
            "extensions": SERVER_EXTENSIONS,
            # Not part of the protocol; the one thing worth knowing about this
            # server that the protocol has no field for.
            "backend": {
                "runtime": "onnxruntime",
                "providers": self._providers,
            },
        }

    @api.get("/v2/health/live")
    def live(self) -> dict[str, bool]:
        return {"live": True}

    @api.get("/v2/health/ready")
    def ready(self) -> dict[str, bool]:
        return {"ready": self._repository.is_dir()}

    @api.get("/v2/repository/index")
    def repository_index(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in e.items() if k != "path"} for e in self._index()]

    @api.get("/v2/models/{name}")
    def model_metadata(self, name: str) -> dict[str, Any]:
        session = self._session(name)
        return self._metadata(
            name, _describe(self._repository / name)["version"], session
        )

    @api.get("/v2/models/{name}/ready")
    def model_ready(self, name: str) -> dict[str, bool]:
        model_dir = self._repository / name
        ready = model_dir.is_dir() and _describe(model_dir)["state"] == "READY"
        return {"name": name, "ready": ready}

    @api.post("/v2/models/{name}/infer")
    async def infer(
        self, name: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        session = self._session(name)
        feeds = self._build_feeds(session, body)
        requested = [o.get("name") for o in body.get("outputs") or [] if o.get("name")]
        names = requested or [o.name for o in session.get_outputs()]

        loop = asyncio.get_running_loop()
        try:
            results = await loop.run_in_executor(self._pool, session.run, names, feeds)
        except Exception as exc:  # noqa: BLE001 - a bad tensor is a 400, not a 500
            raise HTTPException(
                status_code=400, detail=f"Inference failed: {exc}"
            ) from exc

        return {
            "model_name": name,
            "id": body.get("id"),
            "outputs": [self._encode(n, r) for n, r in zip(names, results)],
        }

    # -- tensors ------------------------------------------------------------

    @staticmethod
    def _build_feeds(session, body: dict[str, Any]) -> dict[str, np.ndarray]:
        inputs = body.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            raise HTTPException(status_code=400, detail="Request is missing 'inputs'.")

        expected = {i.name for i in session.get_inputs()}
        feeds: dict[str, np.ndarray] = {}
        for tensor in inputs:
            name = tensor.get("name")
            if name not in expected:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unexpected input '{name}'; model takes {sorted(expected)}.",
                )
            datatype = tensor.get("datatype")
            dtype = DTYPE_TO_NUMPY.get(datatype)
            if dtype is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported datatype '{datatype}' for input '{name}'.",
                )
            shape = tensor.get("shape")
            data = tensor.get("data")
            if shape is None or data is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Input '{name}' needs both 'shape' and 'data'.",
                )
            try:
                feeds[name] = np.asarray(data, dtype=dtype).reshape(shape)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"Input '{name}': {exc}"
                ) from exc

        missing = expected - feeds.keys()
        if missing:
            raise HTTPException(
                status_code=400, detail=f"Missing inputs: {sorted(missing)}."
            )
        return feeds

    @staticmethod
    def _encode(name: str, array: np.ndarray) -> dict[str, Any]:
        array = np.asarray(array)
        datatype = NUMPY_TO_DTYPE.get(array.dtype)
        if datatype is None:
            raise HTTPException(
                status_code=400,
                detail=f"Output '{name}' has unsupported dtype {array.dtype}.",
            )
        return {
            "name": name,
            "datatype": datatype,
            "shape": list(array.shape),
            "data": array.reshape(-1).tolist(),
        }


app = InferenceServer.bind(
    model_repository=os.environ.get("MODEL_REPOSITORY", "/models")
)
