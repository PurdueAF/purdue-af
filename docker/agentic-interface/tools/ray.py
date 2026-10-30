"""Ray tools — free GPUs by worker shape, jobs at Purdue and on NRP, and the
user's own cluster, which their notebooks reach with Ray Client.

Calls carry the caller's own JupyterHub token, so the Ray gateway shows each
user only their own jobs, and runs theirs as them. A job's code reaches it as
a package, as a JobSubmissionClient uploads one, which works at both sites.
"""

import hashlib
import io
import posixpath
import re
import zipfile
from typing import Any, Literal, Optional

import httpx
from config import RAY_GATEWAY_URL
from context import require_user
from errors import (
    AuthError,
    Failure,
    UpstreamError,
    UserError,
    http_error,
    json_body,
    malformed_response,
    response_detail,
    unreachable,
)
from shared import shared_client

_SERVICE = "the Ray gateway"

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ENDED = frozenset({"SUCCEEDED", "FAILED", "STOPPED"})

# Far above any script, far below the pod's memory.
_MAX_CODE_BYTES = 10 * 2**20
# A log is what an agent reads next, so its size is the agent's context.
_MAX_LOG_CHARS = 20_000
# A submission to Purdue answers once the job's cluster is up.
_SUBMIT_TIMEOUT = 300.0


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {require_user()['token']}"}


def _http_failure(resp: httpx.Response, action: str) -> Failure:
    """What a non-2xx gateway answer means: its text is written for the user."""
    code = resp.status_code
    detail = response_detail(resp, limit=500) or "no reason given"
    if code in (401, 403):
        return AuthError(f"Error: not authorised to {action} — {detail}.")
    if code == 429:
        return UserError(
            f"Error: could not {action} — {detail}. get_ray_capacity shows what "
            "is free now."
        )
    if 400 <= code < 500:
        return UserError(f"Error: could not {action} — {detail}.")
    if code == 503:
        return UpstreamError(f"Error: could not {action} — {detail}.")
    return http_error(_SERVICE, resp, action=action)


async def _gateway(
    method: str,
    path: str,
    *,
    action: str,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 30.0,
    **kwargs: Any,
) -> httpx.Response:
    """One gateway call — or the Failure that explains why it could not be made."""
    try:
        resp = await shared_client("ray-gateway").request(
            method,
            f"{RAY_GATEWAY_URL}{path}",
            headers={**_auth(), **(headers or {})},
            timeout=timeout,
            **kwargs,
        )
    except httpx.RequestError as exc:
        raise unreachable(_SERVICE, exc)
    if not resp.is_success:
        raise _http_failure(resp, action)
    return resp


def _job_path(job_id: str) -> str:
    if not _JOB_ID_RE.match(job_id or ""):
        raise UserError(
            f"Error: invalid job ID {job_id!r} — use an ID list_ray_jobs returns."
        )
    return f"/api/jobs/{job_id}"


def _package(files: dict[str, str]) -> tuple[str, bytes]:
    """The name and bytes of the package Ray unpacks into the job's directory."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name in sorted(files):
            path = posixpath.normpath(name)
            if name.startswith("/") or path == ".." or path.startswith("../"):
                raise UserError(
                    f"Error: {name!r} in files is not a path inside the job's "
                    "directory: use a relative path."
                )
            package.writestr(zipfile.ZipInfo(path), files[name])
    data = buffer.getvalue()
    if len(data) > _MAX_CODE_BYTES:
        raise UserError(
            f"Error: files come to {len(data) / 2**20:.1f} MiB compressed, over the "
            f"{_MAX_CODE_BYTES // 2**20} MiB limit. Keep data out of them: name it "
            "with inputs, or read it from /work at Purdue."
        )
    return f"_ray_pkg_{hashlib.sha256(data).hexdigest()[:16]}.zip", data


def _settings(**settings: Any) -> dict[str, str]:
    """The gateway's headers: each setting given, as a string."""
    return {
        f"af-{name.replace('_', '-')}": str(value)
        for name, value in settings.items()
        if value is not None
    }


def _fmt_job(job: dict[str, Any]) -> str:
    lines = [f"**{job.get('submission_id')}**  status={job.get('status')}"]
    for key in ("entrypoint", "message"):
        if job.get(key):
            lines.append(f"  {key}: {job[key]}")
    return "\n".join(lines)


def _fmt_purdue(purdue: dict[str, Any], workers: Optional[int]) -> list[str]:
    if not purdue.get("known"):
        return [
            "Purdue: free workers unknown now, the monitoring system does not "
            f"answer. A job there has at most {purdue.get('workers_most')} workers."
        ]
    lines = [
        f"Purdue: {purdue['workers_free']} workers free now, of at most "
        f"{purdue['workers_most']} for a job."
    ]
    lines += [
        f"  {kind['memory_gb']:g} GB GPU: {kind['workers_free']} free, "
        f"{kind['workers_preemptible']} on GPUs other users' preemptible workers hold"
        for kind in purdue["kinds"]
    ]
    if workers:
        fits = purdue["workers_free"] >= workers
        lines.append(f"  {workers} workers: {'fit now' if fits else 'do not fit now'}.")
    return lines


def _fmt_nrp(nrp: Optional[dict[str, Any]], workers: Optional[int]) -> list[str]:
    if nrp is None:
        return ["NRP: not for this shape, whose workers have one GPU each."]
    if not nrp.get("known"):
        return ["NRP: unknown now, its monitoring shows none of its GPU nodes."]
    size = nrp["worker"]
    lines = [
        f"NRP: {nrp['workers_free']} workers free now for one job, of at most "
        f"{nrp['workers_most']} once its nodes are empty; each has {size['gpus']} "
        f"GPU, {size['cpus']:g} CPU cores and {size['memory_gb']:g} GB. A job takes "
        "one GPU model, the one with the most room, and cannot choose it."
    ]
    lines += [
        f"  {group['workers']} on {group['model']}, {group['region']}"
        for group in nrp["groups"][:5]
    ]
    if workers:
        fits = nrp["workers_free"] >= workers
        lines.append(
            f"  {workers} workers: {'fit now' if fits else 'do not fit now'}"
            + ("." if workers <= nrp["workers_most"] else ", and never would.")
        )
    return lines


def _connect(cluster: dict[str, Any]) -> str:
    """The notebook call that reaches the cluster: other settings would replace it."""
    settings = [("af-max-workers", cluster["workers"])]
    if cluster["gpus_per_worker"] != 1:
        settings.append(("af-gpus-per-worker", cluster["gpus_per_worker"]))
    if cluster["environment"]:
        settings.append(("af-env", cluster["environment"]))
    metadata = ", ".join(f'("{k}", "{v}")' for k, v in settings)
    return f'ray.init("ray://ray-train-gateway:10001", _metadata=[{metadata}])'


def _fmt_cluster(cluster: dict[str, Any]) -> str:
    lines = [
        f"**{cluster['name']}**  ready={cluster['ready']}  busy={cluster['busy']}",
        f"  workers: {cluster['workers_ready']} ready of {cluster['workers']}, "
        f"{cluster['gpus_per_worker']} GPU(s) of {cluster['min_gpu_memory_gb']:g} GB "
        f"or more, {cluster['cpus_per_worker']} CPU cores and "
        f"{cluster['ram_per_worker_gb']} GB each",
        f"  environment: {cluster['environment'] or 'the global environment'}",
        "",
        "From a notebook of a session without a GPU:",
        f"  {_connect(cluster)}",
        "A cluster is removed once idle, with its GPUs.",
    ]
    return "\n".join(lines)


def _cluster_body(resp: httpx.Response) -> dict[str, Any]:
    body = json_body(resp)
    if not isinstance(body, dict) or "exists" not in body:
        raise malformed_response(_SERVICE, resp, "a Ray cluster")
    return body


def register(mcp: Any) -> None:
    @mcp.tool()
    async def get_ray_capacity(
        min_gpu_memory_gb: Optional[float] = None,
        gpus_per_worker: int = 1,
        workers: Optional[int] = None,
    ) -> str:
        """How many Ray workers of a shape are free now, at Purdue and on NRP.

        Args:
            min_gpu_memory_gb: The least memory of a GPU: 16 for T4s at Purdue. Any GPU by default.
            gpus_per_worker: GPUs of each worker, for a model on several GPUs. One by default.
            workers: A number of workers to check against what is free.
        """
        params = _settings(
            min_gpu_memory_gb=min_gpu_memory_gb, gpus_per_worker=gpus_per_worker
        )
        resp = await _gateway(
            "GET", "/af/capacity", action="read Ray capacity", params=params
        )
        body = json_body(resp)
        if not isinstance(body, dict) or "purdue" not in body:
            raise malformed_response(_SERVICE, resp, "Ray capacity")
        lines = [
            *_fmt_purdue(body["purdue"], workers),
            *_fmt_nrp(body.get("nrp"), workers),
            "",
            "Next: submit_ray_job, with workers and site from the above. Free "
            "counts are a moment's reading: a submission is judged when it arrives.",
        ]
        return "\n".join(lines)

    @mcp.tool()
    async def submit_ray_job(
        entrypoint: str,
        site: Literal["purdue", "nrp"] = "purdue",
        files: Optional[dict[str, str]] = None,
        workers: int = 1,
        exact_workers: bool = False,
        entrypoint_gpus: int = 0,
        min_gpu_memory_gb: Optional[float] = None,
        gpus_per_worker: Optional[int] = None,
        cpus_per_worker: Optional[int] = None,
        ram_per_worker_gb: Optional[int] = None,
        environment: Optional[str] = None,
        inputs: Optional[str] = None,
        output: Optional[str] = None,
        pip: Optional[list[str]] = None,
        env_vars: Optional[dict[str, str]] = None,
    ) -> str:
        """Submit a Ray job: training, hyperparameter search or batch inference on GPUs.

        The job runs on GPUs of its own, and goes on whatever happens to the session.
        A script reads the number of workers from AF_MAX_WORKERS.

        Args:
            entrypoint: The command, such as "python train.py".
            site: "purdue", or "nrp" for many more GPUs. A job on NRP sees none of /work, /depot or /eos: it takes code through files and data through inputs and output, on workers of a fixed size in the global environment, and any GPU of it under 40% busy over 30 minutes stops it.
            files: The job's code, {relative path: text}, unpacked into its working directory. At Purdue a script on /work can instead be run with "cd /work/users/<username>/project && python train.py".
            workers: The most workers: the job takes as many as are free.
            exact_workers: Start only if all of workers are free, and keep that number: for a model split across GPUs.
            entrypoint_gpus: GPUs of the entrypoint itself: 1 for a plain PyTorch script that uses cuda, 0 for Ray Train, Ray Tune and Ray Data, which place their own work.
            min_gpu_memory_gb: The least memory of a GPU, at Purdue: 16 for T4s.
            gpus_per_worker: GPUs of each worker, at Purdue: 2 for a model on two GPUs.
            cpus_per_worker: CPU cores of each worker, at Purdue.
            ram_per_worker_gb: Memory of each worker in GB, at Purdue.
            environment: Path of a Pixi or conda environment of the user's with Ray in it, at Purdue. The global environment by default.
            inputs: A directory of AF storage the job reads, in AF_INPUTS: on NRP an s3:// address, copied there first.
            output: A directory inside the user's own on AF storage for results, in AF_OUTPUT: on NRP an s3:// address, copied back when the job ends. Never directly in /work or /depot/cms.
            pip: Packages to add to the environment.
            env_vars: Environment variables of the job.
        """
        count = {"n_workers" if exact_workers else "max_workers": workers}
        headers = _settings(
            site=site,
            min_gpu_memory_gb=min_gpu_memory_gb,
            gpus_per_worker=gpus_per_worker,
            cpus_per_worker=cpus_per_worker,
            ram_per_worker_gb=ram_per_worker_gb,
            env=environment,
            inputs=inputs,
            output=output,
            **count,
        )
        runtime_env: dict[str, Any] = {}
        if files:
            name, data = _package(files)
            await _gateway(
                "PUT",
                f"/api/packages/gcs/{name}",
                action="upload the job's files",
                headers=headers,
                content=data,
                timeout=_SUBMIT_TIMEOUT,
            )
            runtime_env["working_dir"] = f"gcs://{name}"
        if pip:
            runtime_env["pip"] = pip
        if env_vars:
            runtime_env["env_vars"] = env_vars
        submission: dict[str, Any] = {
            "entrypoint": entrypoint,
            "runtime_env": runtime_env,
        }
        if entrypoint_gpus:
            submission["entrypoint_num_gpus"] = entrypoint_gpus
        resp = await _gateway(
            "POST",
            "/api/jobs/",
            action="submit the job",
            headers=headers,
            json=submission,
            timeout=_SUBMIT_TIMEOUT,
        )
        body = json_body(resp)
        if not isinstance(body, dict) or not body.get("submission_id"):
            raise malformed_response(_SERVICE, resp, "a submitted job")
        wait = "up to half an hour" if site == "nrp" else "a minute or two"
        return (
            f"Submitted job {body['submission_id']} at {site}. It can take {wait} "
            f"to start.\nNext: get_ray_job('{body['submission_id']}') for its "
            "status and log."
        )

    @mcp.tool()
    async def list_ray_jobs() -> str:
        """List the calling user's Ray jobs, at Purdue and on NRP, running or ended."""
        resp = await _gateway("GET", "/api/jobs/", action="list your Ray jobs")
        jobs = json_body(resp)
        if not isinstance(jobs, list):
            raise malformed_response(_SERVICE, resp, "a job list")
        if not jobs:
            return "No Ray jobs."
        return f"# {len(jobs)} Ray job(s)\n" + "\n\n".join(_fmt_job(j) for j in jobs)

    @mcp.tool()
    async def get_ray_job(job_id: str, log_lines: int = 100) -> str:
        """Status of a Ray job, why it is in it, and the end of its log.

        Args:
            job_id: The ID submit_ray_job or list_ray_jobs returns.
            log_lines: How many of the log's last lines to return.
        """
        path = _job_path(job_id)
        resp = await _gateway("GET", path, action=f"read job {job_id}")
        job = json_body(resp)
        if not isinstance(job, dict):
            raise malformed_response(_SERVICE, resp, "a job record")
        report = _fmt_job(job)
        if job.get("status") == "PENDING":
            return f"{report}\n\nNext: ask again in a minute; it has no log yet."
        resp = await _gateway("GET", f"{path}/logs", action=f"read job {job_id}'s log")
        body = json_body(resp)
        logs = str(body.get("logs", "")) if isinstance(body, dict) else ""
        tail = "\n".join(logs.splitlines()[-log_lines:])[-_MAX_LOG_CHARS:]
        report += f"\n\n--- log ---\n{tail}" if tail else "\n\nIts log is empty."
        if job.get("status") in _ENDED:
            return f"{report}\n\nNext: delete_ray_job when its record is not needed."
        return f"{report}\n\nNext: ask again later, or stop_ray_job to end it."

    @mcp.tool()
    async def get_ray_cluster() -> str:
        """The calling user's own Ray cluster: its workers, and how to reach it."""
        resp = await _gateway("GET", "/af/cluster", action="read your Ray cluster")
        cluster = _cluster_body(resp)
        if not cluster["exists"]:
            return (
                "You have no Ray cluster.\nNext: start_ray_cluster for interactive "
                "work, or submit_ray_job, which needs none."
            )
        return _fmt_cluster(cluster)

    @mcp.tool()
    async def start_ray_cluster(
        workers: int = 1,
        exact_workers: bool = False,
        min_gpu_memory_gb: Optional[float] = None,
        gpus_per_worker: Optional[int] = None,
        cpus_per_worker: Optional[int] = None,
        ram_per_worker_gb: Optional[int] = None,
        environment: Optional[str] = None,
    ) -> str:
        """Start the user's own Ray cluster at Purdue, for interactive work from a notebook.

        For work that runs by itself, submit_ray_job needs no cluster and goes on
        with no notebook open. A user has one cluster: one of other settings is
        replaced unless it is busy.

        Args:
            workers: The most workers: the cluster takes as many as are free.
            exact_workers: Start only if all of workers are free.
            min_gpu_memory_gb: The least memory of a GPU: 16 for T4s.
            gpus_per_worker: GPUs of each worker.
            cpus_per_worker: CPU cores of each worker.
            ram_per_worker_gb: Memory of each worker in GB.
            environment: Path of a Pixi or conda environment of the user's with Ray in it. The global environment by default.
        """
        count = {"n_workers" if exact_workers else "max_workers": workers}
        headers = _settings(
            min_gpu_memory_gb=min_gpu_memory_gb,
            gpus_per_worker=gpus_per_worker,
            cpus_per_worker=cpus_per_worker,
            ram_per_worker_gb=ram_per_worker_gb,
            env=environment,
            **count,
        )
        resp = await _gateway(
            "POST",
            "/af/cluster",
            action="start your Ray cluster",
            headers=headers,
            timeout=_SUBMIT_TIMEOUT,
        )
        return f"Started.\n{_fmt_cluster(_cluster_body(resp))}"

    @mcp.tool()
    async def stop_ray_cluster(force: bool = False) -> str:
        """Stop the user's own Ray cluster, which frees its GPUs.

        Args:
            force: Stop it even with tasks or jobs running, which are lost.
        """
        await _gateway(
            "DELETE",
            "/af/cluster",
            action="stop your Ray cluster",
            params={"force": "true"} if force else None,
        )
        return "Stopping your Ray cluster. Notebooks connected to it must ray.init() again."

    @mcp.tool()
    async def stop_ray_job(job_id: str) -> str:
        """Stop a running Ray job. It is not resumed: its progress is what it saved.

        Args:
            job_id: The ID submit_ray_job or list_ray_jobs returns.
        """
        path = f"{_job_path(job_id)}/stop"
        await _gateway("POST", path, action=f"stop job {job_id}")
        return (
            f"Stopping job {job_id}.\nNext: get_ray_job to see it end; on NRP its "
            "outputs are copied back first."
        )

    @mcp.tool()
    async def delete_ray_job(job_id: str) -> str:
        """Delete an ended Ray job's record and log, which cannot be read again.

        Args:
            job_id: The ID submit_ray_job or list_ray_jobs returns.
        """
        await _gateway("DELETE", _job_path(job_id), action=f"delete job {job_id}")
        return f"Deleted job {job_id}."
