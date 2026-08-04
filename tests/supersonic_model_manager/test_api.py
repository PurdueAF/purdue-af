"""HTTP surface: auth gating, state assembly and upload handling."""

import base64
import io
import zipfile

import httpx
import pytest
from model_manager import kube, metrics, triton
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
    calls = []

    async def fake_control(name, action, servers=None):
        calls.append(name)
        return load_result(True, [])

    monkeypatch.setattr(settings, "auto_load_on_upload", False)
    monkeypatch.setattr(triton, "control_model", fake_control)

    async with client as c:
        response = await c.post(
            "/api/upload",
            files={"files": ("m.zip", model_zip(), "application/zip")},
            data={"name": "mymodel"},
        )

    assert response.status_code == 200
    assert calls == []
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
