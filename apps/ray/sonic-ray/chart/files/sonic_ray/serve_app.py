"""The Ray Serve deployment: the model repository's ONNX models behind plain
JSON endpoints.

One deployment, ``SonicServer``, holds every ONNX model of the repository on
one GPU — a replica *is* a server. Ray Serve runs as many replicas as the
request load calls for (see ``autoscaling_config`` in the chart's
serveConfigV2), and the Ray autoscaler adds a GPU pod for each replica that
has nowhere to go. That is the whole scaling story; nothing here reads a
metric.

Endpoints::

    GET  /healthz                 200 once the models are loaded
    GET  /models                  {"models": [...], "skipped": {name: reason}}
    GET  /models/{name}           inputs/outputs (names, dtypes, shapes), version
    POST /models/{name}           {"inputs": {name: nested list}} →
                                  {"model", "version", "outputs": {name: nested list}}

Configuration is by environment variable (the chart sets them on every
container)::

    MODEL_REPOSITORY         /models        the Triton-layout repository
    ONNX_EXECUTION_PROVIDERS CUDAExecutionProvider,CPUExecutionProvider
    LOG_LEVEL                INFO
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from ray import serve

from sonic_ray.models import DEFAULT_PROVIDERS, InvalidInput, Model, ModelStore

LOGGER = logging.getLogger("sonic_ray")
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

app = FastAPI(title="sonic-ray")


def _providers_from_env() -> tuple[str, ...]:
    raw = os.environ.get("ONNX_EXECUTION_PROVIDERS", "")
    chosen = tuple(p.strip() for p in raw.split(",") if p.strip())
    return chosen or DEFAULT_PROVIDERS


@serve.deployment
@serve.ingress(app)
class SonicServer:
    """One replica = one GPU = every ONNX model in the repository."""

    def __init__(self) -> None:
        started = time.monotonic()
        self.store = ModelStore(
            os.environ.get("MODEL_REPOSITORY", "/models"), _providers_from_env()
        )
        LOGGER.info(
            "ready in %.1fs: loaded %s; skipped %s",
            time.monotonic() - started,
            sorted(self.store.models) or "nothing",
            self.store.skipped or "nothing",
        )

    @app.get("/healthz")
    async def healthz(self) -> dict[str, int]:
        return {"models": len(self.store.models)}

    @app.get("/models")
    async def list_models(self) -> dict[str, Any]:
        return {"models": sorted(self.store.models), "skipped": self.store.skipped}

    @app.get("/models/{model_name}")
    async def model_metadata(self, model_name: str) -> dict[str, Any]:
        return self._model(model_name).metadata()

    @app.post("/models/{model_name}")
    async def infer(self, http_request: Request, model_name: str) -> dict[str, Any]:
        model = self._model(model_name)
        try:
            body = await http_request.json()
        except ValueError:
            raise HTTPException(
                status_code=400, detail="body is not valid JSON"
            ) from None
        inputs = body.get("inputs") if isinstance(body, dict) else None
        if not isinstance(inputs, dict) or not inputs:
            raise HTTPException(
                status_code=400,
                detail='body must be {"inputs": {<name>: <array>, ...}}',
            )
        started = time.monotonic()
        try:
            # ORT releases the GIL while it runs, so a thread lets the event
            # loop keep accepting requests for the other models meanwhile.
            outputs = await asyncio.to_thread(model.infer, inputs)
        except InvalidInput as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        LOGGER.debug("infer %s %.1fms", model.name, (time.monotonic() - started) * 1000)
        return {
            "model": model.name,
            "version": model.version,
            "outputs": {name: array.tolist() for name, array in outputs.items()},
        }

    def _model(self, name: str) -> Model:
        try:
            return self.store.get(name)
        except KeyError:
            detail = f"unknown model {name!r}"
            if name in self.store.skipped:
                detail = f"model {name!r} is not loaded: {self.store.skipped[name]}"
            raise HTTPException(status_code=404, detail=detail) from None


# What serveConfigV2's import_path points at: `sonic_ray.serve_app:sonic`.
# (ray ships no type information: to mypy the decorated class is still the
# plain class, which has no bind.)
sonic = SonicServer.bind()  # type: ignore[attr-defined]
