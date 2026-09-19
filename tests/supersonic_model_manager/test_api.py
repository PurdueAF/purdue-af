"""HTTP surface: auth gating, state assembly and upload handling."""

import base64
import io
import json
import logging
import zipfile
from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager
from model_manager import kube, metrics, repository, triton
from model_manager.config import settings
from model_manager.main import app


@pytest.fixture
def client(repo, monkeypatch):
    """App client with auth off and no cluster/Prometheus dependencies."""
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(kube, "pvc_capacity_bytes", lambda: 0)
    monkeypatch.setattr(kube, "api_available", lambda: False)

    async def no_servers():
        return {"servers": [], "models": {}}

    async def no_metrics():
        return {"models": {}, "error": None, "configured": False}

    monkeypatch.setattr(triton, "collect_state", no_servers)
    monkeypatch.setattr(metrics, "collect_metrics", no_metrics)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def model_zip(name="mymodel", version_dir=True):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            f"{name}/config.pbtxt", f'name: "{name}"\nplatform: "onnxruntime_onnx"\n'
        )
        zf.writestr(f"{name}/{'1/' if version_dir else ''}model.onnx", "weights")
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@pytest.fixture
def secured(repo, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_username", "admin")
    monkeypatch.setattr(settings, "auth_password", "s3cret")
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.mark.parametrize("path", ["/", "/api/state", "/static/index.html"])
async def test_everything_requires_credentials(secured, path):
    async with secured as client:
        response = await client.get(path)

    assert response.status_code == 401
    assert "Basic" in response.headers["www-authenticate"]


async def test_healthz_stays_open_for_kubelet_probes(secured):
    async with secured as client:
        response = await client.get("/healthz")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "user,password", [("admin", "wrong"), ("wrong", "s3cret"), ("", "")]
)
async def test_bad_credentials_are_rejected(secured, user, password):
    async with secured as client:
        response = await client.get("/api/state", headers=basic(user, password))

    assert response.status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        "Basic !!!not-base64!!!",
        "Basic " + base64.b64encode(b"\xff\xfe").decode(),
        "Bearer abc",
    ],
)
async def test_malformed_authorization_is_rejected(secured, header):
    async with secured as client:
        response = await client.get("/api/state", headers={"Authorization": header})

    assert response.status_code == 401


async def test_non_ascii_credentials_are_rejected_not_crashed(secured):
    """compare_digest raises TypeError on non-ASCII str."""
    async with secured as client:
        response = await client.get("/api/state", headers=basic("admin", "pässwörd"))

    assert response.status_code == 401


async def test_correct_credentials_are_accepted(secured, monkeypatch):
    monkeypatch.setattr(kube, "pvc_capacity_bytes", lambda: 0)

    async def no_servers():
        return {"servers": [], "models": {}}

    async def no_metrics():
        return {"models": {}, "error": None, "configured": False}

    monkeypatch.setattr(triton, "collect_state", no_servers)
    monkeypatch.setattr(metrics, "collect_metrics", no_metrics)

    async with secured as client:
        response = await client.get("/api/state", headers=basic("admin", "s3cret"))

    assert response.status_code == 200


async def test_auth_without_a_password_fails_closed(secured, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "")

    async with secured as client:
        response = await client.get("/api/state")

    assert response.status_code == 500
    assert "AUTH_PASSWORD" in response.json()["error"]


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


async def test_state_lists_models_on_the_pvc(client, make_model):
    make_model("particlenet", size=100)

    async with client as c:
        payload = (await c.get("/api/state")).json()

    assert [m["name"] for m in payload["models"]] == ["particlenet"]
    model = payload["models"][0]
    assert model["source"] == "pvc"
    assert model["loadedCount"] == 0
    assert model["sizeBytes"] > 0


async def test_state_marks_models_only_present_on_servers(client, monkeypatch):
    async def with_server():
        return {
            "servers": [{"name": "t-0", "live": True, "models": [], "error": None}],
            "models": {
                "from_cvmfs": {"t-0": {"state": "READY", "version": "1", "reason": ""}}
            },
        }

    monkeypatch.setattr(triton, "collect_state", with_server)

    async with client as c:
        payload = (await c.get("/api/state")).json()

    model = payload["models"][0]
    assert model["source"] == "external"
    assert model["sizeBytes"] is None, "a server-only model has no PVC footprint"
    assert model["loadedCount"] == 1


async def test_state_marks_models_present_in_both_places(
    client, make_model, monkeypatch
):
    make_model("deepmet")

    async def with_server():
        return {
            "servers": [{"name": "t-0", "live": True, "models": [], "error": None}],
            "models": {
                "deepmet": {"t-0": {"state": "READY", "version": "1", "reason": ""}}
            },
        }

    monkeypatch.setattr(triton, "collect_state", with_server)

    async with client as c:
        payload = (await c.get("/api/state")).json()

    assert payload["models"][0]["source"] == "both"


# --------------------------------------------------------------------------
# Upload / delete
# --------------------------------------------------------------------------


async def test_upload_installs_a_valid_model(client, repo):
    async with client as c:
        response = await c.post(
            "/api/upload",
            files={"files": ("mymodel.zip", model_zip(), "application/zip")},
            data={"name": "mymodel", "overwrite": "false"},
        )

    assert response.status_code == 200, response.text
    assert (repo / "mymodel" / "1" / "model.onnx").is_file()


async def test_upload_of_invalid_model_returns_structured_errors(client, repo):
    async with client as c:
        response = await c.post(
            "/api/upload",
            files={
                "files": ("bad.zip", model_zip(version_dir=False), "application/zip")
            },
            data={"name": "mymodel", "overwrite": "false"},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["validation"]["errors"]
    assert not (repo / "mymodel").exists()


async def test_upload_rejected_in_read_only_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "read_only", True)

    async with client as c:
        response = await c.post(
            "/api/upload",
            files={"files": ("m.zip", model_zip(), "application/zip")},
            data={"name": "mymodel"},
        )

    assert response.status_code == 403


async def test_delete_removes_a_model(client, repo, make_model):
    make_model("doomed")

    async with client as c:
        response = await c.delete("/api/models/doomed")

    assert response.status_code == 200
    assert not (repo / "doomed").exists()


async def test_delete_rejected_in_read_only_mode(client, make_model, monkeypatch):
    make_model("safe")
    monkeypatch.setattr(settings, "read_only", True)

    async with client as c:
        response = await c.delete("/api/models/safe")

    assert response.status_code == 403


async def test_model_control_rejected_in_read_only_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "read_only", True)

    async with client as c:
        response = await c.post("/api/models/m/load", json={})

    assert response.status_code == 403


# --------------------------------------------------------------------------
# Auto-serve on upload
# --------------------------------------------------------------------------


def load_result(ok, results, error=None):
    return {
        "action": "load",
        "model": "mymodel",
        "results": results,
        "ok": ok,
        "error": error,
    }


async def test_upload_serves_the_model_automatically(client, repo, monkeypatch):
    calls = []

    async def fake_control(name, action, servers=None):
        calls.append((name, action))
        return load_result(True, [{"server": "t-0", "ok": True, "error": None}])

    monkeypatch.setattr(settings, "auto_load_on_upload", True)
    monkeypatch.setattr(triton, "control_model", fake_control)

    async with client as c:
        response = await c.post(
            "/api/upload",
            files={"files": ("m.zip", model_zip(), "application/zip")},
            data={"name": "mymodel"},
        )

    assert response.status_code == 200
    assert calls == [("mymodel", "load")]
    auto = response.json()["autoLoad"]
    assert auto["ok"] is True
    assert auto["loadedOn"] == ["t-0"]


async def test_upload_survives_a_server_with_no_room(client, repo, monkeypatch):
    """A model that cannot be loaded still lands on the PVC."""

    async def fake_control(name, action, servers=None):
        return load_result(
            False,
            [
                {"server": "t-0", "ok": True, "error": None},
                {
                    "server": "t-1",
                    "ok": False,
                    "error": "failed to load: CUDA out of memory",
                },
            ],
            error="failed to load: CUDA out of memory",
        )

    monkeypatch.setattr(settings, "auto_load_on_upload", True)
    monkeypatch.setattr(triton, "control_model", fake_control)

    async with client as c:
        response = await c.post(
            "/api/upload",
            files={"files": ("m.zip", model_zip(), "application/zip")},
            data={"name": "mymodel"},
        )

    assert response.status_code == 200, "a failed load must not fail the upload"
    assert (repo / "mymodel" / "1" / "model.onnx").is_file()
    auto = response.json()["autoLoad"]
    assert auto["ok"] is False
    assert auto["loadedOn"] == ["t-0"]
    assert "out of memory" in auto["failedOn"][0]["error"]


async def test_auto_load_can_be_disabled(client, repo, monkeypatch):
    control = AsyncMock()
    monkeypatch.setattr(settings, "auto_load_on_upload", False)
    monkeypatch.setattr(triton, "control_model", control)

    async with client as c:
        response = await c.post(
            "/api/upload",
            files={"files": ("m.zip", model_zip(), "application/zip")},
            data={"name": "mymodel"},
        )

    assert response.status_code == 200
    control.assert_not_awaited()
    assert response.json()["autoLoad"] is None


async def test_staging_directory_is_not_listed_as_a_model(client, repo, monkeypatch):
    """Triton indexes every subdirectory, including our upload staging area."""

    async def with_staging():
        return {
            "servers": [{"name": "t-0", "live": True, "models": [], "error": None}],
            "models": {},
        }

    monkeypatch.setattr(triton, "collect_state", with_staging)
    (repo / ".uploads").mkdir()

    async with client as c:
        payload = (await c.get("/api/state")).json()

    assert [m["name"] for m in payload["models"]] == []


async def test_index_serves_the_dashboard(client):
    async with client as c:
        response = await c.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_state_merges_servers_metrics_and_versions(client, monkeypatch):
    async def with_servers():
        return {
            "servers": [
                {"name": "t-0", "live": True, "models": [], "error": None},
                {"name": "t-1", "live": False, "models": [], "error": "down"},
            ],
            "models": {
                "ext": {
                    "t-0": {"state": "READY", "version": "2", "reason": ""},
                    "t-1": {"state": "UNAVAILABLE", "version": "1", "reason": ""},
                }
            },
        }

    async def with_metrics():
        return {
            "models": {"ext": {"throughput": 4.0}},
            "error": "batchRatio: HTTPStatusError",
            "configured": True,
            "window": "5m",
        }

    monkeypatch.setattr(triton, "collect_state", with_servers)
    monkeypatch.setattr(metrics, "collect_metrics", with_metrics)

    async with client as c:
        payload = (await c.get("/api/state")).json()

    assert payload["serverNames"] == ["t-0", "t-1"]
    assert payload["liveServerCount"] == 1
    model = payload["models"][0]
    assert model["versions"] == ["1", "2"]
    assert model["loadedOn"] == ["t-0"]
    assert model["knownToServers"] == ["t-0", "t-1"]
    assert model["metrics"] == {"throughput": 4.0}
    assert payload["prometheus"]["error"] == "batchRatio: HTTPStatusError"
    assert payload["prometheus"]["window"] == "5m"


async def test_state_survives_a_stray_unicode_digit_directory(client, make_model):
    """'²'.isdigit() is True but int('²') raises."""
    model = make_model("m")
    (model / "²").mkdir()

    async with client as c:
        response = await c.get("/api/state")

    assert response.status_code == 200
    assert response.json()["models"][0]["versions"] == ["1"]


async def test_lifespan_creates_the_repository_and_drops_stale_staging(
    tmp_path, monkeypatch
):
    root = tmp_path / "fresh" / "models"
    monkeypatch.setattr(settings, "repository_path", str(root))
    cleaned = []
    monkeypatch.setattr(repository, "cleanup_staging", lambda: cleaned.append(True))

    async with LifespanManager(app):
        assert root.is_dir()

    assert cleaned == [True]


async def test_lifespan_tolerates_an_unwritable_repository(
    tmp_path, monkeypatch, caplog
):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(settings, "repository_path", str(blocker / "models"))

    with caplog.at_level(logging.WARNING, logger="model_manager"):
        async with LifespanManager(app):
            pass

    assert "not writable" in caplog.text


# --------------------------------------------------------------------------
# Load / unload
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["load", "unload"])
async def test_control_passes_the_requested_servers(client, monkeypatch, action):
    control = AsyncMock(return_value=load_result(True, []))
    monkeypatch.setattr(triton, "control_model", control)

    async with client as c:
        response = await c.post(f"/api/models/m/{action}", json={"servers": ["t-1"]})

    assert response.status_code == 200
    control.assert_awaited_once_with("m", action, ["t-1"])


@pytest.mark.parametrize("body", [b"not json", b'["t-1"]'])
async def test_control_without_a_usable_body_targets_every_server(
    client, monkeypatch, body
):
    control = AsyncMock(return_value=load_result(True, []))
    monkeypatch.setattr(triton, "control_model", control)

    async with client as c:
        await c.post("/api/models/m/load", content=body)

    control.assert_awaited_once_with("m", "load", None)


async def test_failed_control_is_a_bad_gateway(client, monkeypatch):
    monkeypatch.setattr(
        triton,
        "control_model",
        AsyncMock(return_value=load_result(False, [], error="boom")),
    )

    async with client as c:
        response = await c.post("/api/models/m/load")

    assert response.status_code == 502
    assert response.json()["error"] == "boom"


async def test_delete_of_a_missing_model_is_a_bad_request(client):
    async with client as c:
        response = await c.delete("/api/models/ghost")

    assert response.status_code == 400
    assert "not present" in response.json()["detail"]


# --------------------------------------------------------------------------
# Upload errors
# --------------------------------------------------------------------------


async def test_upload_larger_than_the_limit_is_refused(client, repo, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 16)

    async with client as c:
        response = await c.post(
            "/api/upload",
            files={"files": ("m.zip", model_zip(), "application/zip")},
            data={"name": "mymodel"},
        )

    assert response.status_code == 400
    assert "size limit" in response.json()["detail"]
    assert list((repo / repository.STAGING_DIRNAME).iterdir()) == []


async def test_upload_over_an_existing_model_needs_overwrite(client, repo, make_model):
    make_model("mymodel")

    async with client as c:
        response = await c.post(
            "/api/upload",
            files={"files": ("m.zip", model_zip(), "application/zip")},
            data={"name": "mymodel"},
        )

    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]


async def test_unexpected_upload_failure_is_a_server_error(client, monkeypatch):
    def explode(*args):
        raise ValueError("disk on fire")

    monkeypatch.setattr(repository, "install_archive", explode)

    async with client as c:
        response = await c.post(
            "/api/upload",
            files={"files": ("m.zip", model_zip(), "application/zip")},
            data={"name": "mymodel"},
        )

    assert response.status_code == 500
    assert "disk on fire" in response.json()["detail"]


async def test_upload_to_an_unwritable_repository_is_a_server_error(
    client, tmp_path, monkeypatch
):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(settings, "repository_path", str(blocker))

    async with client as c:
        response = await c.post(
            "/api/upload",
            files={"files": ("m.zip", model_zip(), "application/zip")},
        )

    assert response.status_code == 500
    assert "not writable" in response.json()["detail"]


# --------------------------------------------------------------------------
# Directory upload (browser directory picker)
# --------------------------------------------------------------------------

CONFIG = b'name: "dirmodel"\nplatform: "onnxruntime_onnx"\n'


def directory_files(*entries):
    return [("files", (name.rsplit("/", 1)[-1], data)) for name, data in entries]


async def test_directory_upload_takes_the_name_from_the_folder(client, repo):
    entries = [("dirmodel/config.pbtxt", CONFIG), ("dirmodel/1/model.onnx", b"w")]

    async with client as c:
        response = await c.post(
            "/api/upload",
            files=directory_files(*entries),
            data={"paths": json.dumps([name for name, _ in entries])},
        )

    assert response.status_code == 200, response.text
    assert response.json()["uploaded"]["name"] == "dirmodel"
    assert (repo / "dirmodel" / "1" / "model.onnx").read_bytes() == b"w"
    assert not (repo / "dirmodel" / "dirmodel").exists()


async def test_directory_upload_under_an_explicit_name(client, repo):
    async with client as c:
        response = await c.post(
            "/api/upload",
            files=directory_files(
                ("x", CONFIG.replace(b"dirmodel", b"renamed")), ("y", b"w")
            ),
            data={
                "name": "renamed",
                "paths": json.dumps(["./config.pbtxt", "\\1\\model.onnx"]),
            },
        )

    assert response.status_code == 200, response.text
    assert (repo / "renamed" / "1" / "model.onnx").is_file()


async def test_invalid_directory_upload_is_structured_and_cleaned_up(client, repo):
    entries = [("dirmodel/config.pbtxt", CONFIG), ("dirmodel/model.onnx", b"w")]

    async with client as c:
        response = await c.post(
            "/api/upload",
            files=directory_files(*entries),
            data={"paths": json.dumps([name for name, _ in entries])},
        )

    assert response.status_code == 422
    assert response.json()["validation"]["errors"]
    assert not (repo / "dirmodel").exists()
    assert list((repo / repository.STAGING_DIRNAME).iterdir()) == []


async def test_failed_directory_write_cleans_up_staging(client, repo):
    async with client as c:
        response = await c.post(
            "/api/upload",
            files=directory_files(("a", CONFIG), ("b", b"w")),
            data={"name": "dirmodel", "paths": json.dumps(["ok", "../escape"])},
        )

    assert response.status_code == 400
    assert "Unsafe path" in response.json()["detail"]
    assert list((repo / repository.STAGING_DIRNAME).iterdir()) == []


@pytest.mark.parametrize(
    "data,detail",
    [
        ({"paths": "{not json"}, "Malformed"),
        ({"paths": "7"}, "Malformed"),
        ({"paths": json.dumps(["only-one"])}, "does not match"),
        ({"paths": json.dumps(["config.pbtxt", "model.onnx"])}, "Provide a model name"),
        ({}, "Provide a model name"),
    ],
)
async def test_directory_upload_rejects_unusable_form_fields(client, data, detail):
    async with client as c:
        response = await c.post(
            "/api/upload",
            files=directory_files(("config.pbtxt", CONFIG), ("model.onnx", b"w")),
            data=data,
        )

    assert response.status_code == 400
    assert detail in response.json()["detail"]


async def test_upload_with_no_files_is_a_400(client):
    """FastAPI rejects a body with no file field before the handler; the
    handler still guards an empty list for a client that sends the field
    empty."""
    from fastapi import HTTPException
    from model_manager import main

    async with client:
        with pytest.raises(HTTPException) as exc:
            await main.upload(
                request=None, files=[], name="", paths="", overwrite=False
            )
    assert exc.value.status_code == 400
    assert "No files" in exc.value.detail


async def test_http_errors_raised_while_spooling_keep_their_status(client, monkeypatch):
    from fastapi import HTTPException
    from model_manager import main

    async def too_big(*args):
        raise HTTPException(status_code=413, detail="over the limit")

    monkeypatch.setattr(main, "_spool_to_disk", too_big)
    async with client as c:
        response = await c.post(
            "/api/upload",
            files={"files": ("m.zip", model_zip(), "application/zip")},
            data={"name": "mymodel"},
        )
    assert response.status_code == 413
    assert response.json()["detail"] == "over the limit"
