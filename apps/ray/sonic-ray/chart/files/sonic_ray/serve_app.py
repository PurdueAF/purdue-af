"""The Ray Serve deployment: the SuperSONIC model repository on the KServe v2
HTTP endpoints Triton clients already speak.

One deployment, ``SonicServer``, holds every servable model of the
repository on one GPU — the same unit Triton is: a replica *is* a server.
Ray Serve runs as many replicas as the request load calls for (see the
``autoscaling_config`` in the chart's serveConfigV2), and the Ray autoscaler
adds a GPU pod for each replica that has nowhere to go. That is the whole
scaling story; nothing here reads a metric.

Configuration is by environment variable (the chart sets them on every
container), so the package runs anywhere::

    MODEL_REPOSITORY         /models        the Triton-layout repository
    ONNX_EXECUTION_PROVIDERS CUDAExecutionProvider,CPUExecutionProvider
    MODELS                   (unset)        comma-separated allowlist of
                                            directories to load; unset = all
    LOG_LEVEL                INFO
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from ray import serve

from sonic_ray.protocol import (
    HEADER_LENGTH,
    ProtocolError,
    encode_infer_response,
    parse_infer_request,
)
from sonic_ray.repository import (
    DEFAULT_PROVIDERS,
    InvalidInput,
    Model,
    ModelRepository,
    ModelUnavailable,
    UnknownModel,
)

SERVER_NAME = "sonic-ray"
SERVER_VERSION = "0.1.0"
# The v2 extensions this server implements, as Triton advertises its own.
EXTENSIONS = ["binary_data", "model_repository"]

LOGGER = logging.getLogger("sonic_ray")
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

app = FastAPI(title=SERVER_NAME, version=SERVER_VERSION)


def _providers_from_env() -> tuple[str, ...]:
    raw = os.environ.get("ONNX_EXECUTION_PROVIDERS", "")
    chosen = tuple(p.strip() for p in raw.split(",") if p.strip())
    return chosen or DEFAULT_PROVIDERS


def _allowlist_from_env() -> set[str] | None:
    raw = os.environ.get("MODELS", "")
    names = {m.strip() for m in raw.split(",") if m.strip()}
    return names or None


def _preload_cuda_libraries() -> None:
    """onnxruntime ≥ 1.21 can dlopen the CUDA/cuDNN libraries it needs before
    the CUDA provider is created, and reports clearly when it cannot — far
    better than the provider silently falling back to CPU."""
    import onnxruntime as ort

    preload = getattr(ort, "preload_dlls", None)
    if preload is not None:
        try:
            preload()
        except Exception as exc:  # a CPU-only box, or a partial install
            LOGGER.warning("onnxruntime.preload_dlls failed: %s", exc)


@serve.deployment
@serve.ingress(app)
class SonicServer:
    """One replica = one GPU = every servable model in the repository."""

    def __init__(self) -> None:
        root = os.environ.get("MODEL_REPOSITORY", "/models")
        providers = _providers_from_env()
        if any("CUDA" in p or "Tensorrt" in p for p in providers):
            _preload_cuda_libraries()
        started = time.monotonic()
        self.repository = ModelRepository(root, providers, only=_allowlist_from_env())
        ready = self.repository.ready()
        LOGGER.info(
            "%s ready in %.1fs: %d/%d models loaded (%s) with providers %s",
            SERVER_NAME,
            time.monotonic() - started,
            len(ready),
            len(self.repository.entries),
            ", ".join(ready) or "none",
            list(providers),
        )

    # -- health & metadata -------------------------------------------------

    @app.get("/v2/health/live")
    async def live(self) -> Response:
        return Response(status_code=200)

    @app.get("/v2/health/ready")
    async def ready(self) -> Response:
        # Like Triton without --strict-readiness: the server is ready when it
        # is up, even if some models are not (the index says which).
        return Response(status_code=200)

    @app.get("/v2")
    async def server_metadata(self) -> dict[str, Any]:
        return {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
            "extensions": EXTENSIONS,
        }

    @app.get("/v2/models/{model_name}")
    @app.get("/v2/models/{model_name}/versions/{model_version}")
    async def model_metadata(
        self, model_name: str, model_version: str | None = None
    ) -> dict[str, Any]:
        return self._model(model_name, model_version).metadata()

    @app.get("/v2/models/{model_name}/ready")
    @app.get("/v2/models/{model_name}/versions/{model_version}/ready")
    async def model_ready(
        self, model_name: str, model_version: str | None = None
    ) -> Response:
        self._model(model_name, model_version)
        return Response(status_code=200)

    @app.get("/v2/repository/index")
    @app.post("/v2/repository/index")
    async def repository_index(self) -> list[dict[str, Any]]:
        return self.repository.index()

    # -- inference ---------------------------------------------------------

    @app.post("/v2/models/{model_name}/infer")
    @app.post("/v2/models/{model_name}/versions/{model_version}/infer")
    async def infer(
        self, http_request: Request, model_name: str, model_version: str | None = None
    ) -> Response:
        model = self._model(model_name, model_version)
        body = await http_request.body()
        try:
            request = parse_infer_request(body, http_request.headers.get(HEADER_LENGTH))
        except ProtocolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        started = time.monotonic()
        try:
            # ORT releases the GIL while it runs, so a thread lets the event
            # loop keep accepting requests for the other models meanwhile.
            outputs = await asyncio.to_thread(model.infer, request.inputs)
        except InvalidInput as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        LOGGER.debug(
            "infer %s id=%s batch=%s %.1fms",
            model.name,
            request.id,
            next(iter(request.inputs.values())).shape[0]
            if model.config.batched
            else "-",
            (time.monotonic() - started) * 1000,
        )
        payload, headers = encode_infer_response(
            request, model.name, model.version, outputs
        )
        return Response(content=payload, headers=headers)

    # -- helpers -----------------------------------------------------------

    def _model(self, name: str, version: str | None) -> Model:
        try:
            return self.repository.get(name, version)
        except UnknownModel:
            raise HTTPException(
                status_code=404, detail=f"unknown model {name!r}"
            ) from None
        except ModelUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None


# What serveConfigV2's import_path points at: `sonic_ray.serve_app:sonic`.
# (ray ships no type information: to mypy the decorated class is still the
# plain class, which has no bind.)
sonic = SonicServer.bind()  # type: ignore[attr-defined]
