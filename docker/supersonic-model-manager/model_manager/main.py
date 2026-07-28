"""SuperSONIC Model Manager — FastAPI backend.

Serves the dashboard, exposes the model repository on the PVC, and proxies
load/unload calls to the Triton servers of a SuperSONIC release.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import kube, metrics, repository, triton
from .auth import BasicAuthMiddleware
from .config import settings

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("model_manager")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    root = repository.repo_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Model repository %s is not writable: %s", root, exc)
    await asyncio.to_thread(repository.cleanup_staging)
    log.info(
        "Model repository: %s | Triton discovery: %s | Prometheus: %s",
        root,
        settings.triton_discovery,
        settings.prometheus_url or "(not configured)",
    )
    yield


app = FastAPI(
    title="SuperSONIC Model Manager", docs_url=None, redoc_url=None, lifespan=lifespan
)
app.add_middleware(BasicAuthMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _require_writable() -> None:
    if settings.read_only:
        raise HTTPException(
            status_code=403, detail="This instance is running in read-only mode."
        )


# --------------------------------------------------------------------------
# Read paths
# --------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api/state")
async def state() -> dict:
    (
        pvc_capacity,
        models_on_pvc,
        triton_state,
        prom,
        inference,
        grafana,
    ) = await asyncio.gather(
        asyncio.to_thread(kube.pvc_capacity_bytes),
        asyncio.to_thread(repository.scan_models),
        triton.collect_state(),
        metrics.collect_metrics(),
        asyncio.to_thread(kube.find_inference_endpoint),
        asyncio.to_thread(kube.find_grafana_url),
    )
    storage = await asyncio.to_thread(repository.storage_usage, pvc_capacity)

    servers = triton_state["servers"]
    server_names = [s["name"] for s in servers]
    live_servers = [s["name"] for s in servers if s["live"]]

    by_name = {entry.name: entry.to_dict() for entry in models_on_pvc}
    rows = []
    for name in sorted(set(by_name) | set(triton_state["models"]), key=str.lower):
        pvc_entry = by_name.get(name)
        server_states = triton_state["models"].get(name, {})
        loaded = [
            server
            for server, info in server_states.items()
            if str(info.get("state", "")).upper() == "READY"
        ]
        if pvc_entry and server_states:
            source = "both"
        elif pvc_entry:
            source = "pvc"
        else:
            source = "external"

        rows.append(
            {
                "name": name,
                "source": source,
                "sizeBytes": pvc_entry["sizeBytes"] if pvc_entry else None,
                "fileCount": pvc_entry["fileCount"] if pvc_entry else None,
                "versions": pvc_entry["versions"]
                if pvc_entry
                else sorted(
                    {
                        info.get("version", "")
                        for info in server_states.values()
                        if info.get("version")
                    }
                ),
                "platform": pvc_entry["platform"] if pvc_entry else "",
                "modified": pvc_entry["modified"] if pvc_entry else None,
                "hasConfig": pvc_entry["hasConfig"] if pvc_entry else None,
                "serverStates": server_states,
                "loadedOn": sorted(loaded),
                "loadedCount": len(loaded),
                "knownToServers": sorted(server_states),
                "metrics": prom["models"].get(name, {}),
            }
        )

    return {
        "instance": settings.instance_name,
        "namespace": settings.namespace,
        "readOnly": settings.read_only,
        "refreshSeconds": settings.refresh_seconds,
        "maxUploadBytes": settings.max_upload_bytes,
        "storage": storage,
        "pvcName": settings.pvc_name,
        "inference": inference,
        "grafana": grafana,
        "servers": servers,
        "serverNames": server_names,
        "liveServerCount": len(live_servers),
        "models": rows,
        "prometheus": {
            "configured": prom.get("configured", False),
            "url": settings.prometheus_url,
            "window": prom.get("window"),
            "error": prom.get("error"),
        },
        "kubernetes": {
            "available": kube.api_available(),
            "discovery": settings.triton_discovery,
        },
        "updatedAt": time.time(),
    }


# --------------------------------------------------------------------------
# Model control
# --------------------------------------------------------------------------


async def _control(name: str, action: str, request: Request) -> JSONResponse:
    _require_writable()
    try:
        body = await request.json()
    except Exception:
        body = {}
    servers = body.get("servers") if isinstance(body, dict) else None
    result = await triton.control_model(name, action, servers)
    return JSONResponse(result, status_code=200 if result["ok"] else 502)


@app.post("/api/models/{name}/load")
async def load_model(name: str, request: Request) -> JSONResponse:
    return await _control(name, "load", request)


@app.post("/api/models/{name}/unload")
async def unload_model(name: str, request: Request) -> JSONResponse:
    return await _control(name, "unload", request)


@app.delete("/api/models/{name}")
async def delete_model(name: str) -> dict:
    _require_writable()
    try:
        await asyncio.to_thread(repository.delete_model, name)
    except repository.RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted": name}


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------


async def _spool_to_disk(upload: UploadFile, destination: Path, limit: int) -> int:
    """Stream an upload straight onto the PVC so large models never buffer in RAM."""
    written = 0
    with open(destination, "wb") as out:
        while True:
            chunk = await upload.read(4 * 1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise repository.RepositoryError(
                    "Upload exceeds the configured size limit "
                    f"({limit / (1024**3):.1f} GiB)."
                )
            out.write(chunk)
    return written


@app.post("/api/upload")
async def upload(
    request: Request,
    files: list[UploadFile],
    name: str = Form(""),
    paths: str = Form(""),
    overwrite: bool = Form(False),
) -> dict:
    _require_writable()
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")

    staging_root = repository.repo_root() / repository.STAGING_DIRNAME
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"Model repository is not writable: {exc}"
        )

    # A single archive is the common case; a set of files with relative paths
    # comes from the browser's directory picker.
    single_archive = len(files) == 1 and repository.is_archive(files[0].filename or "")

    try:
        if single_archive:
            upload_file = files[0]
            tmp = (
                staging_root
                / f"incoming-{uuid.uuid4().hex}-{Path(upload_file.filename).name}"
            )
            try:
                await _spool_to_disk(upload_file, tmp, settings.max_upload_bytes)
                result = await asyncio.to_thread(
                    repository.install_archive,
                    tmp,
                    upload_file.filename or "",
                    name.strip(),
                    overwrite,
                )
            finally:
                tmp.unlink(missing_ok=True)
        else:
            relative_paths = []
            if paths:
                try:
                    relative_paths = json.loads(paths)
                except json.JSONDecodeError:
                    raise repository.RepositoryError("Malformed 'paths' field.")
            if len(relative_paths) not in (0, len(files)):
                raise repository.RepositoryError(
                    "'paths' does not match the uploaded files."
                )

            effective_name = name.strip()
            if not effective_name and relative_paths:
                first = str(relative_paths[0]).replace("\\", "/").lstrip("/")
                effective_name = first.split("/")[0] if "/" in first else ""
            if not effective_name:
                raise repository.RepositoryError(
                    "Provide a model name, or upload a directory / archive that contains one."
                )

            upload_dir = repository.DirectoryUpload(effective_name, overwrite)
            try:
                for index, upload_file in enumerate(files):
                    relative = (
                        str(relative_paths[index])
                        if relative_paths
                        else (upload_file.filename or f"file-{index}")
                    )
                    # Drop the leading model directory: it becomes the model name.
                    parts = [
                        p
                        for p in relative.replace("\\", "/").split("/")
                        if p not in ("", ".")
                    ]
                    if len(parts) > 1 and parts[0] == effective_name:
                        parts = parts[1:]
                    await asyncio.to_thread(
                        upload_dir.add_file, "/".join(parts), upload_file.file
                    )
                result = await asyncio.to_thread(upload_dir.finish)
            except Exception:
                upload_dir.cleanup()
                raise
    except repository.ValidationFailed as exc:
        # Structured so the dashboard can list every problem at once.
        return JSONResponse(
            status_code=422,
            content={
                "detail": "The upload is not a valid Triton model.",
                "validation": exc.result.to_dict(),
            },
        )
    except repository.RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Upload failed")
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")

    log.info("Installed model %s (%s bytes)", result["name"], result["sizeBytes"])
    return {"uploaded": result}
