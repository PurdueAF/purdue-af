"""Tests for tools/ray.py — capacity and jobs through the Ray gateway."""

import io
import json
import zipfile

import httpx
import pytest
import respx
from agentic_helpers import failure, register_tools
from context import current_user
from tools import ray

GATEWAY = ray.RAY_GATEWAY_URL
JOBS = f"{GATEWAY}/api/jobs/"
CAPACITY = f"{GATEWAY}/af/capacity"
CLUSTER = f"{GATEWAY}/af/cluster"

PURDUE = {
    "known": True,
    "workers_free": 5,
    "workers_preemptible": 3,
    "workers_most": 22,
    "kinds": [
        {
            "resource": "nvidia.com/gpu",
            "memory_gb": 16.0,
            "workers_most": 8,
            "workers_free": 5,
            "workers_preemptible": 3,
        }
    ],
}
NRP = {
    "known": True,
    "worker": {"gpus": 1, "cpus": 4.0, "memory_gb": 16.0},
    "workers_free": 12,
    "workers_most": 95,
    "groups": [{"region": "us-west", "model": "NVIDIA A10", "workers": 12}],
}


RUNNING = {
    "exists": True,
    "name": "ray-train-alice",
    "ready": True,
    "busy": False,
    "workers": 4,
    "workers_ready": 3,
    "min_gpu_memory_gb": 16.0,
    "gpus_per_worker": 2,
    "cpus_per_worker": "8",
    "ram_per_worker_gb": "32",
    "environment": "/work/users/alice/env",
}


@pytest.fixture
def tools():
    token = current_user.set({"username": "alice", "token": "tok-alice"})
    yield register_tools(ray).tools
    current_user.reset(token)


@respx.mock
async def test_capacity_names_the_shape_and_both_sites(tools):
    route = respx.get(CAPACITY).respond(200, json={"purdue": PURDUE, "nrp": NRP})
    out = await tools["get_ray_capacity"](min_gpu_memory_gb=16, workers=8)
    query = route.calls.last.request.url.params
    assert query["af-min-gpu-memory-gb"] == "16"
    assert query["af-gpus-per-worker"] == "1"
    assert "Purdue: 5 workers free" in out
    assert "8 workers: do not fit now" in out
    assert "NRP: 12 workers free" in out
    assert "8 workers: fit now" in out
    assert "NVIDIA A10" in out


@respx.mock
async def test_capacity_says_when_a_number_could_never_fit_on_nrp(tools):
    respx.get(CAPACITY).respond(200, json={"purdue": PURDUE, "nrp": NRP})
    out = await tools["get_ray_capacity"](workers=100)
    assert "never would" in out


@respx.mock
async def test_capacity_tells_unknown_from_none(tools):
    purdue = {"known": False, "workers_most": 22}
    respx.get(CAPACITY).respond(
        200, json={"purdue": purdue, "nrp": {"known": False, "worker": NRP["worker"]}}
    )
    out = await tools["get_ray_capacity"]()
    assert "Purdue: free workers unknown" in out
    assert "NRP: unknown" in out


@respx.mock
async def test_capacity_of_a_shape_nrp_has_no_workers_of(tools):
    respx.get(CAPACITY).respond(200, json={"purdue": PURDUE, "nrp": None})
    out = await tools["get_ray_capacity"](gpus_per_worker=2)
    assert "NRP: not for this shape" in out


@respx.mock
async def test_capacity_passes_on_the_gateways_refusal(tools):
    respx.get(CAPACITY).respond(400, text="No GPU of a Ray cluster has 99 GB")
    message = await failure(tools["get_ray_capacity"](min_gpu_memory_gb=99))
    assert "No GPU of a Ray cluster has 99 GB" in message


@respx.mock
async def test_a_failure_to_reach_the_gateway_names_it(tools):
    respx.get(JOBS).mock(side_effect=httpx.ConnectError("refused"))
    assert "unreachable" in await failure(tools["list_ray_jobs"]())


@respx.mock
async def test_a_malformed_answer_is_a_failure(tools):
    respx.get(CAPACITY).respond(200, json=[])
    assert "not Ray capacity" in await failure(tools["get_ray_capacity"]())


@respx.mock
async def test_submit_at_purdue_sends_the_settings_as_the_users_own(tools):
    route = respx.post(JOBS).respond(200, json={"submission_id": "raysubmit_a"})
    out = await tools["submit_ray_job"](
        "python train.py",
        workers=4,
        exact_workers=True,
        min_gpu_memory_gb=16,
        gpus_per_worker=2,
        cpus_per_worker=8,
        ram_per_worker_gb=32,
        environment="/work/users/alice/env",
        entrypoint_gpus=1,
        pip=["tabulate"],
        env_vars={"A": "1"},
    )
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer tok-alice"
    assert request.headers["af-site"] == "purdue"
    assert request.headers["af-n-workers"] == "4"
    assert "af-max-workers" not in request.headers
    assert request.headers["af-min-gpu-memory-gb"] == "16"
    assert request.headers["af-gpus-per-worker"] == "2"
    assert request.headers["af-cpus-per-worker"] == "8"
    assert request.headers["af-ram-per-worker-gb"] == "32"
    assert request.headers["af-env"] == "/work/users/alice/env"
    assert json.loads(request.content) == {
        "entrypoint": "python train.py",
        "runtime_env": {"pip": ["tabulate"], "env_vars": {"A": "1"}},
        "entrypoint_num_gpus": 1,
    }
    assert "raysubmit_a" in out and "get_ray_job" in out


@respx.mock
async def test_submit_to_nrp_uploads_the_files_with_the_same_settings(tools):
    uploads = respx.put(url__startswith=f"{GATEWAY}/api/packages/gcs/").respond(200)
    route = respx.post(JOBS).respond(200, json={"submission_id": "raysubmit_b"})
    out = await tools["submit_ray_job"](
        "python train.py",
        site="nrp",
        files={"train.py": "print(1)\n", "lib/a.py": "x = 1\n"},
        workers=2,
        inputs="/work/users/alice/events",
        output="/work/users/alice/results",
    )
    upload = uploads.calls.last.request
    with zipfile.ZipFile(io.BytesIO(upload.content)) as package:
        assert sorted(package.namelist()) == ["lib/a.py", "train.py"]
        assert package.read("train.py") == b"print(1)\n"
    request = route.calls.last.request
    for header in ("af-site", "af-max-workers", "af-inputs", "af-output"):
        assert upload.headers[header] == request.headers[header]
    name = upload.url.path.rpartition("/")[2]
    assert name.startswith("_ray_pkg_") and name.endswith(".zip")
    assert json.loads(request.content)["runtime_env"] == {
        "working_dir": f"gcs://{name}"
    }
    assert "half an hour" in out


@pytest.mark.parametrize("name", ["/etc/passwd", "../up.py", "a/../../up.py"])
async def test_files_stay_in_the_jobs_directory(tools, name):
    message = await failure(tools["submit_ray_job"]("python a.py", files={name: ""}))
    assert "relative path" in message


async def test_files_have_a_size_limit(tools, monkeypatch):
    monkeypatch.setattr(ray, "_MAX_CODE_BYTES", 10)
    message = await failure(
        tools["submit_ray_job"]("python a.py", files={"a.py": "x = 1\n" * 100})
    )
    assert "limit" in message


@respx.mock
@pytest.mark.parametrize(
    ("code", "kind"), [(429, "get_ray_capacity"), (400, "could not"), (403, "not auth")]
)
async def test_a_refused_submission_says_why(tools, code, kind):
    respx.post(JOBS).respond(code, text="Ray clusters can take 0 more now")
    message = await failure(tools["submit_ray_job"]("python a.py"))
    assert "Ray clusters can take 0 more now" in message
    assert kind in message


@respx.mock
async def test_a_gateway_that_is_down_is_not_the_users_fault(tools):
    respx.post(JOBS).respond(503, text="NRP did not take your job")
    message = await failure(tools["submit_ray_job"]("python a.py", site="nrp"))
    assert "NRP did not take your job" in message


@respx.mock
async def test_a_gateway_fault_is_reported_as_one(tools):
    respx.get(JOBS).respond(500, text="boom")
    assert "HTTP 500" in await failure(tools["list_ray_jobs"]())


@respx.mock
async def test_a_malformed_submission_answer_is_a_failure(tools):
    respx.post(JOBS).respond(200, json={})
    assert "a submitted job" in await failure(tools["submit_ray_job"]("python a.py"))


@respx.mock
async def test_list_shows_each_job(tools):
    respx.get(JOBS).respond(
        200,
        json=[
            {
                "submission_id": "raysubmit_a",
                "status": "RUNNING",
                "entrypoint": "python train.py",
                "message": "",
            }
        ],
    )
    out = await tools["list_ray_jobs"]()
    assert "raysubmit_a" in out and "status=RUNNING" in out


@respx.mock
async def test_list_says_when_there_are_none(tools):
    respx.get(JOBS).respond(200, json=[])
    assert await tools["list_ray_jobs"]() == "No Ray jobs."


@respx.mock
async def test_a_malformed_list_is_a_failure(tools):
    respx.get(JOBS).respond(200, json={})
    assert "a job list" in await failure(tools["list_ray_jobs"]())


@respx.mock
async def test_a_job_shows_its_status_and_the_end_of_its_log(tools):
    respx.get(f"{JOBS}raysubmit_a").respond(
        200, json={"submission_id": "raysubmit_a", "status": "RUNNING"}
    )
    respx.get(f"{JOBS}raysubmit_a/logs").respond(
        200, json={"logs": "\n".join(f"line {i}" for i in range(9))}
    )
    out = await tools["get_ray_job"]("raysubmit_a", log_lines=2)
    assert "line 8" in out and "line 7" in out and "line 6" not in out
    assert "stop_ray_job" in out


@respx.mock
async def test_a_pending_job_asks_for_no_log(tools):
    respx.get(f"{JOBS}raysubmit_a").respond(
        200,
        json={
            "submission_id": "raysubmit_a",
            "status": "PENDING",
            "message": "Copying its inputs to NRP.",
        },
    )
    out = await tools["get_ray_job"]("raysubmit_a")
    assert "Copying its inputs to NRP." in out and "no log yet" in out


@respx.mock
async def test_an_ended_job_with_no_log_says_so(tools):
    respx.get(f"{JOBS}raysubmit_a").respond(
        200,
        json={
            "submission_id": "raysubmit_a",
            "status": "STOPPED",
            "message": "Its idlest GPU was 0% busy",
        },
    )
    respx.get(f"{JOBS}raysubmit_a/logs").respond(200, json={"logs": ""})
    out = await tools["get_ray_job"]("raysubmit_a")
    assert "Its idlest GPU" in out and "log is empty" in out
    assert "delete_ray_job" in out


@respx.mock
async def test_a_malformed_job_is_a_failure(tools):
    respx.get(f"{JOBS}raysubmit_a").respond(200, json=[])
    assert "a job record" in await failure(tools["get_ray_job"]("raysubmit_a"))


@respx.mock
async def test_an_unknown_job_is_the_users_to_fix(tools):
    respx.get(f"{JOBS}raysubmit_a").respond(404, text="You have no job raysubmit_a")
    assert "You have no job" in await failure(tools["get_ray_job"]("raysubmit_a"))


@pytest.mark.parametrize("tool", ["get_ray_job", "stop_ray_job", "delete_ray_job"])
async def test_a_job_id_is_checked_before_it_reaches_a_path(tools, tool):
    assert "invalid job ID" in await failure(tools[tool]("../clusters"))


@respx.mock
async def test_stop_and_delete_call_the_job(tools):
    stop = respx.post(f"{JOBS}raysubmit_a/stop").respond(200, json={"stopped": True})
    delete = respx.delete(f"{JOBS}raysubmit_a").respond(200, json={"deleted": True})
    assert "Stopping" in await tools["stop_ray_job"]("raysubmit_a")
    assert "Deleted" in await tools["delete_ray_job"]("raysubmit_a")
    assert stop.called and delete.called


@respx.mock
async def test_a_cluster_shows_its_workers_and_how_to_reach_it(tools):
    respx.get(CLUSTER).respond(200, json=RUNNING)
    out = await tools["get_ray_cluster"]()
    assert "3 ready of 4" in out
    assert '("af-max-workers", "4")' in out
    assert '("af-gpus-per-worker", "2")' in out
    assert '("af-env", "/work/users/alice/env")' in out


@respx.mock
async def test_no_cluster_points_to_starting_one(tools):
    respx.get(CLUSTER).respond(200, json={"exists": False})
    out = await tools["get_ray_cluster"]()
    assert "no Ray cluster" in out and "start_ray_cluster" in out


@respx.mock
async def test_starting_a_cluster_sends_its_settings(tools):
    route = respx.post(CLUSTER).respond(
        200, json=RUNNING | {"gpus_per_worker": 1, "environment": ""}
    )
    out = await tools["start_ray_cluster"](
        workers=4, exact_workers=True, min_gpu_memory_gb=16, cpus_per_worker=8
    )
    headers = route.calls.last.request.headers
    assert headers["authorization"] == "Bearer tok-alice"
    assert headers["af-n-workers"] == "4"
    assert headers["af-cpus-per-worker"] == "8"
    assert "af-site" not in headers
    assert out.startswith("Started.") and "the global environment" in out
    assert "af-gpus-per-worker" not in out


@respx.mock
async def test_a_refused_cluster_points_to_capacity(tools):
    respx.post(CLUSTER).respond(429, text="Ray clusters can take 0 more now")
    message = await failure(tools["start_ray_cluster"]())
    assert "Ray clusters can take 0 more now" in message
    assert "get_ray_capacity" in message


@respx.mock
async def test_a_malformed_cluster_is_a_failure(tools):
    respx.get(CLUSTER).respond(200, json=[])
    assert "a Ray cluster" in await failure(tools["get_ray_cluster"]())


@respx.mock
async def test_stopping_a_cluster_forces_only_when_asked(tools):
    route = respx.delete(CLUSTER).respond(200, json={"exists": False})
    assert "Stopping" in await tools["stop_ray_cluster"]()
    assert "force" not in route.calls.last.request.url.params
    await tools["stop_ray_cluster"](force=True)
    assert route.calls.last.request.url.params["force"] == "true"
