"""Dask cluster tools — create, list, inspect, scale, stop via Gateway API.

The gateway uses SimpleAuthenticator (password ignored). Calls authenticate as
the Hub username via HTTP Basic so each user only sees their own clusters.

Worker counts come from the AF Prometheus (dask_scheduler_workers). Live CPU /
memory usage comes from the cluster Prometheus (cadvisor), filtered to Running
worker pods.
"""

import asyncio
import base64
import re
import time
from typing import Any, Optional

import httpx
from config import (
    CLUSTER_PROMETHEUS_URL,
    DASK_GATEWAY_URL,
    GLOBAL_PIXI_PROJECT,
    PROMETHEUS_URL,
)
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
from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field
from shared import prom_query, prom_scalar, prom_vector, quote_label, shared_client

from tools.elicitation import ask

_SERVICE = "Dask Gateway"


# Mirror apps/dask-gateway/values.yaml.
MAX_WORKERS = 200
_WORKER_CORES = (0.1, 64.0)
_WORKER_MEMORY = (0.1, 64.0)


def _check_worker_size(worker_cores: float, worker_memory: float) -> None:
    """Raise UserError if the per-worker size exceeds the gateway's configured
    option limits. The gateway enforces these too; checking here turns a 422
    round-trip into an immediate, precise message."""
    lo, hi = _WORKER_CORES
    if not lo <= worker_cores <= hi:
        raise UserError(f"Error: worker_cores must be between {lo:g} and {hi:g}.")
    lo, hi = _WORKER_MEMORY
    if not lo <= worker_memory <= hi:
        raise UserError(f"Error: worker_memory must be between {lo:g} and {hi:g} GiB.")


# Bounds the create/scale wait for a PENDING scheduler; the gateway refuses to
# scale a cluster that is not RUNNING.
SCHEDULER_READY_TIMEOUT = 120.0
SCHEDULER_POLL_INTERVAL = 3.0

# Statuses from which a cluster will never reach RUNNING — waiting is pointless.
_TERMINAL_STATUSES = frozenset({"STOPPING", "STOPPED", "FAILED"})


# Cluster names land in gateway URL paths and (via _cluster_id) in PromQL
# regexes, so only accept the character set the gateway emits.
_CLUSTER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_cluster_name(cluster_name: str) -> None:
    """Raise UserError if ``cluster_name`` is not a safe name."""
    if not _CLUSTER_NAME_RE.match(cluster_name or ""):
        raise UserError(
            f"Error: invalid cluster name {cluster_name!r} — use a name "
            "returned by list_dask_clusters."
        )


def _auth(username: str) -> dict:
    """HTTP Basic for SimpleAuthenticator (password field ignored when unset)."""
    cred = base64.b64encode(f"{username}:".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


# ── failure reporting ─────────────────────────────────────────────────────────


def _gateway_http_error(
    resp: httpx.Response,
    action: str,
    cluster_name: Optional[str] = None,
) -> Failure:
    """What a non-2xx gateway answer means for this user.

    The gateway authenticates by Hub username, so a refusal is about the
    user's access, not about the token; a 404 is about the cluster name.
    """
    code = resp.status_code
    if code in (401, 403):
        return AuthError(
            f"Error: not authorised on {_SERVICE} to {action} (HTTP {code}). "
            "If you believe you should have access, contact AF support."
        )
    if code == 404 and cluster_name:
        return UserError(
            f"Cluster '{cluster_name}' not found. Call list_dask_clusters for "
            "the current names."
        )
    if code in (409, 422):
        return UserError(
            f"Error: {_SERVICE} rejected the request to {action} — "
            f"{response_detail(resp, limit=400) or 'no reason given'}."
        )
    return http_error(_SERVICE, resp, action=action)


def _cluster_id(cluster_name: str) -> str:
    """Strip the namespace prefix from a gateway cluster name.

    ``cms.ec5c698a…`` → ``ec5c698a…`` (matches dask-scheduler-/dask-worker- pods).
    """
    return cluster_name.rsplit(".", 1)[-1]


def _parse_clusters(payload: Any) -> list[dict[str, Any]]:
    """Normalise GET /api/v1/clusters/ body to a list of cluster dicts.

    Dask Gateway returns ``{cluster_name: cluster_model, …}``.
    """
    if isinstance(payload, dict):
        return [c for c in payload.values() if isinstance(c, dict)]
    if isinstance(payload, list):
        return [c for c in payload if isinstance(c, dict)]
    return []


def _fmt_cluster(c: dict) -> str:
    name = c.get("name", "?")
    status = c.get("status", "?")
    workers = c.get("workers") or {}
    n_workers = len(workers) if isinstance(workers, dict) else int(workers or 0)
    adaptive = c.get("adaptive")
    scale_info = (
        f"  adaptive({adaptive.get('minimum', '?')}–{adaptive.get('maximum', '?')})"
        if adaptive
        else f"  workers={n_workers}"
    )
    scheduler = c.get("scheduler_address", "")
    lines = [f"**{name}**  status={status}{scale_info}"]
    if scheduler:
        lines.append(f"  scheduler: {scheduler}")
    return "\n".join(lines)


async def _cluster_status(cluster_name: str, username: str) -> str:
    """The gateway's current status word for ``cluster_name`` (upper-case)."""
    resp = await _gateway(
        "GET",
        f"/api/v1/clusters/{cluster_name}",
        username=username,
        action=f"check the state of cluster '{cluster_name}'",
        cluster_name=cluster_name,
    )
    record = json_body(resp)
    if not isinstance(record, dict):
        raise malformed_response(_SERVICE, resp, "a cluster record")
    return str(record.get("status") or "UNKNOWN").upper()


async def _await_scheduler(
    cluster_name: str, username: str, timeout: Optional[float] = None
) -> tuple[str, float]:
    """Poll until ``cluster_name`` is RUNNING, or the wait runs out.

    Returns ``(last_status, seconds_waited)``; the caller decides what a
    still-PENDING cluster means for it. A cluster that has entered a terminal
    state raises instead — no amount of waiting brings it back.

    An already-RUNNING cluster costs exactly one GET, so this is safe to call
    on the normal path.

    The bounds are read at call time, not bound as defaults, so tests can
    shorten them.
    """
    deadline = time.monotonic() + (
        SCHEDULER_READY_TIMEOUT if timeout is None else timeout
    )
    start = time.monotonic()
    while True:
        status = await _cluster_status(cluster_name, username)
        if status == "RUNNING":
            return status, time.monotonic() - start
        if status in _TERMINAL_STATUSES:
            raise UserError(
                f"Error: cluster '{cluster_name}' is {status} — it will not "
                "accept workers. Call list_dask_clusters to confirm, then "
                "create_dask_cluster for a fresh one; query_dask_logs shows why "
                "the scheduler stopped."
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return status, time.monotonic() - start
        await asyncio.sleep(min(SCHEDULER_POLL_INTERVAL, remaining))


async def _gateway(
    method: str,
    path: str,
    *,
    username: str,
    action: str,
    cluster_name: Optional[str] = None,
    ok: tuple[int, ...] = (200,),
    timeout: float = 10.0,
    json: Any = None,
) -> httpx.Response:
    """One gateway API call — or the Failure that explains why it could not
    be made. ``ok`` lists the statuses the caller handles itself."""
    try:
        resp = await shared_client("dask-gateway").request(
            method,
            f"{DASK_GATEWAY_URL}{path}",
            headers=_auth(username),
            json=json,
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        raise unreachable(_SERVICE, exc)
    if resp.status_code not in ok:
        raise _gateway_http_error(resp, action, cluster_name)
    return resp


async def _require_owned_cluster(username: str, cluster_name: str) -> None:
    """Raise a Failure if the user cannot access ``cluster_name``."""
    await _gateway(
        "GET",
        f"/api/v1/clusters/{cluster_name}",
        username=username,
        action=f"access cluster '{cluster_name}'",
        cluster_name=cluster_name,
    )


async def _prom_scalar(
    client: httpx.AsyncClient, base_url: str, query: str
) -> tuple[Optional[float], Optional[str]]:
    """(first scalar or None, problem or None) — see shared.prom_query."""
    rows, problem = await prom_query(client, base_url, query)
    return prom_scalar(rows), problem


async def _prom_vector(
    client: httpx.AsyncClient, base_url: str, query: str
) -> tuple[list[tuple[dict, float]], Optional[str]]:
    """((labels, value) rows, problem or None) — see shared.prom_query."""
    rows, problem = await prom_query(client, base_url, query)
    return prom_vector(rows), problem


def _stats(values: list[float]) -> Optional[tuple[float, float, float]]:
    if not values:
        return None
    return min(values), max(values), sum(values) / len(values)


def _base_worker_env(username: str, extra: Optional[dict] = None) -> dict:
    """Build the env mapping the gateway's options handler requires.

    The handler always pops ``PATH`` and prepends the conda/pixi bin dir, so
    PATH must be present. Callers can override/extend via ``extra``.
    """
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": f"/home/{username}",
        "USER": username,
        "LOGNAME": username,
    }
    if extra:
        for key, value in extra.items():
            if value is None:
                continue
            env[str(key)] = str(value)
    return env


def _build_cluster_options(
    *,
    username: str,
    pixi_project: Optional[str],
    pixi_env: str,
    conda_env: Optional[str],
    worker_cores: float,
    worker_memory: float,
    env: Optional[dict],
) -> dict:
    """Validate create args (raising UserError) and return the Gateway
    ``cluster_options`` body."""
    pixi = (pixi_project or "").strip()
    conda = (conda_env or "").strip()
    if pixi and conda:
        raise UserError(
            "Error: pixi_project and conda_env are mutually exclusive — "
            "specify only one."
        )
    if not pixi and not conda:
        raise UserError(
            "Error: provide either pixi_project (directory with pixi.toml) "
            "or conda_env (path to a conda/pixi env prefix)."
        )
    if worker_cores <= 0:
        raise UserError("Error: worker_cores must be > 0.")
    if worker_memory <= 0:
        raise UserError("Error: worker_memory must be > 0 (GiB).")

    options: dict = {
        "worker_cores": worker_cores,
        "worker_memory": worker_memory,
        "env": _base_worker_env(username, env),
    }
    if pixi:
        options["pixi_project"] = pixi
        options["pixi_env"] = (pixi_env or "default").strip() or "default"
        options["conda_env"] = ""
    else:
        options["conda_env"] = conda
        options["pixi_project"] = ""
        options["pixi_env"] = "default"
    return options


# ── Elicitation schemas (rendered as multiple-choice forms by capable clients) ─


class _EnvChoice(BaseModel):
    """Which worker environment to use."""

    env_source: str = Field(
        default="global",
        json_schema_extra={"enum": ["global", "pixi", "conda"]},
        description=(
            "global = shared pixi env at /work/pixi/global; "
            "pixi = your own pixi project; conda = your own conda env."
        ),
    )


class _PixiChoice(BaseModel):
    """Location of a user-provided pixi project."""

    pixi_project: str = Field(
        description="Path to a pixi project directory (the folder with pixi.toml)."
    )
    pixi_env: str = Field(
        default="default", description="Pixi environment name within the project."
    )


class _CondaChoice(BaseModel):
    """Location of a user-provided conda environment."""

    conda_env: str = Field(
        description="Absolute path to a conda/mamba environment prefix."
    )


# Default worker size when the user picks "default".
DEFAULT_WORKER_CORES = 1.0
DEFAULT_WORKER_MEMORY = 4.0


class _SizeChoice(BaseModel):
    """How big each worker should be."""

    size: str = Field(
        default="default",
        json_schema_extra={"enum": ["default", "custom"]},
        description=(
            f"default = {DEFAULT_WORKER_CORES:g} core / "
            f"{DEFAULT_WORKER_MEMORY:g} GiB per worker; "
            "custom = specify your own cores and memory."
        ),
    )


class _CustomSize(BaseModel):
    """Custom per-worker resources."""

    worker_cores: float = Field(
        gt=0,
        description=f"Cores per worker ({_WORKER_CORES[0]:g}–{_WORKER_CORES[1]:g}).",
    )
    worker_memory: float = Field(
        gt=0,
        description=(
            f"Memory per worker in GiB ({_WORKER_MEMORY[0]:g}–{_WORKER_MEMORY[1]:g})."
        ),
    )


class _CountChoice(BaseModel):
    """How many workers to start the cluster with."""

    count: str = Field(
        default="0",
        json_schema_extra={"enum": ["0", "10", "50", "custom"]},
        description=(
            "Number of workers to start with: 0 (scale later), 10, 50, or "
            "custom to enter your own number."
        ),
    )


class _CustomCount(BaseModel):
    """Custom starting worker count."""

    n_workers: int = Field(ge=0, description="Number of workers to start with.")


# Returned when a choice cannot be elicited; agent clients may auto-decline,
# so a non-accept never proves the user said no.
_CREATE_CHOICES_HELP = (
    "create_dask_cluster needs these choices from the user. Ask them (use the "
    "client's multiple-choice UI if available), then call create_dask_cluster "
    "again with explicit arguments:\n"
    "1) worker environment — one of:\n"
    "   • global (default): shared pixi env at /work/pixi/global — pass "
    "env_source='global'.\n"
    "   • your pixi project: pass pixi_project='/path' (+ optional pixi_env).\n"
    "   • your conda env: pass conda_env='/path'.\n"
    "2) worker size: default (1 core / 4 GiB) or custom (pass worker_cores + "
    "worker_memory in GiB).\n"
    "3) worker count to start with: 0, 10, 50, or a custom number (pass "
    "n_workers)."
)


# Workers register asynchronously and the count comes from a scrape.
_WORKERS_PENDING_NEXT = [
    "",
    "Workers start asynchronously: the pods are scheduled first, then register "
    "with the scheduler. get_dask_worker_count reads Prometheus, so its numbers "
    "trail reality by up to one scrape interval — a low or zero count in the "
    "first minute is expected, not a failed scale.",
    "Next: get_dask_worker_count after ~30-60 s. If it is still short after "
    "that, query_dask_logs shows what the workers are doing and "
    "get_facility_health shows whether the facility is short of capacity.",
]


def register(mcp: Any) -> None:
    @mcp.tool()
    async def list_dask_clusters() -> str:
        """List the calling user's running Dask clusters."""
        resp = await _gateway(
            "GET",
            "/api/v1/clusters/",
            username=require_user()["username"],
            action="list clusters",
        )
        payload = json_body(resp)
        if not isinstance(payload, (dict, list)):
            raise malformed_response(_SERVICE, resp, "a cluster list")
        clusters = _parse_clusters(payload)
        if not clusters:
            return "No running Dask clusters."
        return f"# {len(clusters)} Dask cluster(s)\n" + "\n\n".join(
            _fmt_cluster(c) for c in clusters
        )

    @mcp.tool()
    async def list_dask_cluster_options() -> str:
        """List the create-time options accepted by Dask Gateway.

        Call this before create_dask_cluster to see field names, defaults, and
        limits.
        """
        resp = await _gateway(
            "GET",
            "/api/v1/options",
            username=require_user()["username"],
            action="list cluster options",
        )
        payload = json_body(resp)
        if not isinstance(payload, dict):
            raise malformed_response(_SERVICE, resp, "a cluster-options document")
        fields = payload.get("cluster_options") or []
        lines = [
            "# Dask cluster options",
            "Pass these as arguments to create_dask_cluster.",
            "",
        ]
        for field in fields:
            name = field.get("field", "?")
            label = field.get("label", name)
            default = field.get("default")
            spec = field.get("spec") or {}
            lines.append(f"- {name}: {label}")
            lines.append(f"    default={default!r}  type={spec}")
        lines += [
            "",
            "Notes:",
            "  • Provide exactly one of pixi_project or conda_env.",
            "  • worker_memory is in GiB.",
            "  • Only one active cluster per user is allowed.",
        ]
        return "\n".join(lines)

    @mcp.tool()
    async def create_dask_cluster(
        ctx: Context,
        env_source: Optional[str] = None,
        pixi_project: Optional[str] = None,
        pixi_env: str = "default",
        conda_env: Optional[str] = None,
        worker_cores: Optional[float] = None,
        worker_memory: Optional[float] = None,
        n_workers: Optional[int] = None,
        env: Optional[dict] = None,
    ) -> str:
        """Create a new Dask Gateway cluster.

        Any choice not supplied is asked interactively via the client's
        multiple-choice UI (MCP elicitation), one question at a time:
        environment → worker size → worker count. If a choice can't be
        collected (the client doesn't support elicitation, or the prompt is
        declined or dismissed), a short instruction listing the choices is
        returned instead — collect them from the user and call again with
        explicit args.

        Worker environment (``env_source``):
          • 'global' — shared pixi env at /work/pixi/global
          • 'pixi' — your own pixi project (set ``pixi_project`` + ``pixi_env``)
          • 'conda' — your own conda env (set ``conda_env``)

        Passing ``pixi_project`` or ``conda_env`` directly implies the matching
        ``env_source``. Passing ``worker_cores``/``worker_memory`` skips the size
        question; passing ``n_workers`` skips the count question.

        Args:
            env_source: 'global', 'pixi', or 'conda'. Elicited if omitted and no
                        pixi_project/conda_env is given.
            pixi_project: Path to a pixi project directory.
            pixi_env: Pixi environment name within the project (default 'default').
            conda_env: Path to a conda/mamba env prefix (mutually exclusive with
                       pixi_project).
            worker_cores: Cores per worker (0.1–64). Defaults to 1 if the user
                          picks the default size.
            worker_memory: Memory per worker in GiB (0.1–64). Defaults to 4 if
                           the user picks the default size.
            n_workers: Workers to start with (0–200). 0 (or omitted with a
                       non-eliciting client) starts the cluster empty. A
                       non-zero count waits for the scheduler to come up
                       before scaling, so the call takes as long as the
                       cluster takes to start.
            env: Extra environment variables for workers (e.g. X509_USER_PROXY,
                 PYTHONPATH, NB_UID/NB_GID for CERN/FNAL users).
        """
        if n_workers is not None and n_workers < 0:
            raise UserError("Error: n_workers must be ≥ 0.")
        if n_workers is not None and n_workers > MAX_WORKERS:
            raise UserError(f"Error: n_workers must be ≤ {MAX_WORKERS}.")
        if worker_cores is not None and worker_cores <= 0:
            raise UserError("Error: worker_cores must be > 0.")
        if worker_memory is not None and worker_memory <= 0:
            raise UserError("Error: worker_memory must be > 0 (GiB).")

        help_text = _CREATE_CHOICES_HELP

        # ── Worker environment: infer from explicit paths, else ask the user ──
        if pixi_project:
            env_source = "pixi"
        elif conda_env:
            env_source = "conda"
        elif env_source is None:
            choice = await ask(
                ctx, "Choose the worker environment.", _EnvChoice, help_text
            )
            env_source = choice.env_source

        if env_source == "global":
            pixi_project = GLOBAL_PIXI_PROJECT
            pixi_env = "default"
        elif env_source == "pixi":
            if not pixi_project:
                choice = await ask(
                    ctx,
                    "Provide the path to your pixi project.",
                    _PixiChoice,
                    help_text,
                )
                pixi_project, pixi_env = choice.pixi_project, choice.pixi_env
        elif env_source == "conda":
            if not conda_env:
                choice = await ask(
                    ctx,
                    "Provide the path to your conda environment.",
                    _CondaChoice,
                    help_text,
                )
                conda_env = choice.conda_env
        else:
            raise UserError(
                f"Error: unknown env_source '{env_source}'. "
                "Use 'global', 'pixi', or 'conda'."
            )

        # ── Worker size: ask only if neither cores nor memory was supplied ──
        if worker_cores is None and worker_memory is None:
            choice = await ask(ctx, "Choose the worker size.", _SizeChoice, help_text)
            if choice.size == "custom":
                size = await ask(
                    ctx, "Specify the resources per worker.", _CustomSize, help_text
                )
                worker_cores, worker_memory = size.worker_cores, size.worker_memory
        if worker_cores is None:
            worker_cores = DEFAULT_WORKER_CORES
        if worker_memory is None:
            worker_memory = DEFAULT_WORKER_MEMORY
        _check_worker_size(worker_cores, worker_memory)

        # ── Worker count: ask only if n_workers was not supplied ──
        if n_workers is None:
            choice = await ask(
                ctx,
                "How many workers should the cluster start with?",
                _CountChoice,
                help_text,
            )
            if choice.count == "custom":
                count = await ask(
                    ctx,
                    "Specify the number of workers to start with.",
                    _CustomCount,
                    help_text,
                )
                n_workers = count.n_workers
                if n_workers > MAX_WORKERS:
                    raise UserError(f"Error: n_workers must be ≤ {MAX_WORKERS}.")
            else:
                n_workers = int(choice.count)

        username = require_user()["username"]
        options = _build_cluster_options(
            username=username,
            pixi_project=pixi_project,
            pixi_env=pixi_env,
            conda_env=conda_env,
            worker_cores=worker_cores,
            worker_memory=worker_memory,
            env=env,
        )

        resp = await _gateway(
            "POST",
            "/api/v1/clusters/",
            username=username,
            action="create the cluster",
            ok=(200, 201),
            timeout=60.0,
            json={"cluster_options": options},
        )
        payload = json_body(resp)
        cluster_name = payload.get("name", "") if isinstance(payload, dict) else ""
        if not cluster_name:
            malformed = malformed_response(
                _SERVICE, resp, "a cluster record with a name"
            )
            raise UpstreamError(
                f"{malformed} The cluster may have been created anyway — check "
                "list_dask_clusters before creating another."
            )

        lines = [
            f"Cluster '{cluster_name}' created.",
            f"workers: cores={worker_cores} memory={worker_memory} GiB each",
        ]
        if options.get("pixi_project"):
            lines.append(
                f"env: pixi_project={options['pixi_project']} "
                f"pixi_env={options['pixi_env']}"
            )
        else:
            lines.append(f"env: conda_env={options['conda_env']}")

        if not n_workers:
            lines += [
                "",
                "Cluster starts with 0 workers. Next: scale_dask_cluster(...).",
            ]
            return "\n".join(lines)

        # The cluster exists whatever happens next, so anything that goes wrong
        # from here is reported inside the result rather than as a failure of
        # the call — the caller still needs the name.
        try:
            status, waited = await _await_scheduler(cluster_name, username)
        except Failure as exc:
            lines += [
                "",
                f"Created with 0 workers — waiting for the scheduler failed: {exc}",
            ]
            return "\n".join(lines)

        if status != "RUNNING":
            lines += [
                "",
                f"Created with 0 workers: the scheduler was still {status} after "
                f"{waited:.0f} s, and the gateway only accepts workers once the "
                "cluster is RUNNING.",
                f"Next: scale_dask_cluster('{cluster_name}', {n_workers}) — it "
                "waits for the scheduler too, so calling it again is usually all "
                "that is needed.",
            ]
            return "\n".join(lines)

        try:
            await _gateway(
                "POST",
                f"/api/v1/clusters/{cluster_name}/scale",
                username=username,
                action=f"scale to {n_workers} worker(s)",
                cluster_name=cluster_name,
                ok=(200, 204),
                timeout=30.0,
                json={"count": n_workers},
            )
        except Failure as exc:
            lines += [
                "",
                f"Created with 0 workers — the scale request failed: {exc}",
                f"Retry with scale_dask_cluster('{cluster_name}', {n_workers}).",
            ]
            return "\n".join(lines)

        if waited >= SCHEDULER_POLL_INTERVAL:
            lines.append(f"Scheduler became RUNNING after {waited:.0f} s.")
        lines.append(f"Scaling to {n_workers} worker(s).")
        lines += _WORKERS_PENDING_NEXT
        return "\n".join(lines)

    @mcp.tool()
    async def get_dask_cluster_info(cluster_name: str) -> str:
        """Get detailed information about a specific Dask cluster.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
        """
        _validate_cluster_name(cluster_name)
        user = require_user()
        resp = await _gateway(
            "GET",
            f"/api/v1/clusters/{cluster_name}",
            username=user["username"],
            action=f"inspect cluster '{cluster_name}'",
            cluster_name=cluster_name,
        )
        c = json_body(resp)
        if not isinstance(c, dict):
            raise malformed_response(_SERVICE, resp, "a cluster record")
        workers = c.get("workers") or {}
        worker_lines: list[str] = []
        if isinstance(workers, dict):
            for wname, winfo in list(workers.items())[:20]:
                state = winfo.get("status", "?") if isinstance(winfo, dict) else "?"
                worker_lines.append(f"  {wname}: {state}")
            if len(workers) > 20:
                worker_lines.append(f"  … {len(workers) - 20} more")

        opts = c.get("options", {})
        sections = [_fmt_cluster(c)]
        if opts:
            sections.append(
                "Options:\n" + "\n".join(f"  {k}: {v}" for k, v in opts.items())
            )
        if worker_lines:
            sections.append(f"Workers ({len(workers)}):\n" + "\n".join(worker_lines))
        return "\n\n".join(sections)

    @mcp.tool()
    async def get_dask_worker_count(cluster_name: str) -> str:
        """Return the current number of workers for a Dask cluster (by state).

        Uses the scheduler's Prometheus metrics. Prefer this over guessing from
        list_dask_clusters when you need an accurate live count.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
        """
        _validate_cluster_name(cluster_name)
        username = require_user()["username"]
        await _require_owned_cluster(username, cluster_name)

        cid = _cluster_id(cluster_name)
        quser = quote_label(username)
        sched_pod = f"dask-scheduler-{cid}"
        total_q = f'sum(dask_scheduler_workers{{user="{quser}",pod="{sched_pod}"}})'
        by_state_q = (
            f"sum by (state) ("
            f'dask_scheduler_workers{{user="{quser}",pod="{sched_pod}"}})'
        )
        desired_q = (
            f'sum(dask_scheduler_desired_workers{{user="{quser}",pod="{sched_pod}"}})'
        )

        prom = shared_client("prometheus")
        (total, p1), (by_state, p2), (desired, p3) = await asyncio.gather(
            _prom_scalar(prom, PROMETHEUS_URL, total_q),
            _prom_vector(prom, PROMETHEUS_URL, by_state_q),
            _prom_scalar(prom, PROMETHEUS_URL, desired_q),
        )

        problem = p1 or p2 or p3
        if problem:
            raise UpstreamError(
                f"Error: could not read worker metrics for '{cluster_name}' — "
                f"Prometheus {problem}. The cluster itself may be fine: "
                "get_dask_cluster_info shows the gateway's own view of its workers."
            )
        if total is None:
            return (
                f"No worker metrics for cluster '{cluster_name}' "
                "(scheduler may still be starting, or metrics are stale)."
            )

        lines = [
            f"# Workers for {cluster_name}",
            f"total: {int(total)}",
        ]
        if desired is not None:
            lines.append(f"desired: {int(desired)}")
        state_parts = [
            f"{(m.get('state') or '?')}={int(v)}"
            for m, v in sorted(by_state, key=lambda x: x[0].get("state") or "")
            if v
        ]
        if state_parts:
            lines.append("by state: " + ", ".join(state_parts))
        return "\n".join(lines)

    @mcp.tool()
    async def get_dask_cluster_usage(cluster_name: str) -> str:
        """CPU and memory usage across Running workers of a Dask cluster.

        Reports per-worker min / max / average for CPU (cores) and memory (GiB),
        plus cluster totals. Scoped to the calling user's cluster.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
        """
        _validate_cluster_name(cluster_name)
        username = require_user()["username"]
        await _require_owned_cluster(username, cluster_name)

        cid = _cluster_id(cluster_name)
        # re.escape: '.' is a regex metachar and legitimately appears in names.
        worker_re = f"dask-worker-{re.escape(cid)}-.+"
        # Only Running pods — cadvisor keeps series for terminated workers.
        running = (
            f'kube_pod_status_phase{{namespace="cms",phase="Running",'
            f'pod=~"{worker_re}"}}'
        )
        cpu_q = (
            f"sum by (pod) ("
            f'rate(container_cpu_usage_seconds_total{{namespace="cms",'
            f'pod=~"{worker_re}",container="dask-worker"}}[2m])'
            f" * on(namespace,pod) group_left {running})"
        )
        mem_q = (
            f"sum by (pod) ("
            f'container_memory_working_set_bytes{{namespace="cms",'
            f'pod=~"{worker_re}",container="dask-worker"}}'
            f" * on(namespace,pod) group_left {running})"
        )

        prom = shared_client("cluster-prometheus")
        (cpu_rows, p1), (mem_rows, p2) = await asyncio.gather(
            _prom_vector(prom, CLUSTER_PROMETHEUS_URL, cpu_q),
            _prom_vector(prom, CLUSTER_PROMETHEUS_URL, mem_q),
        )
        problem = p1 or p2
        if problem:
            raise UpstreamError(
                f"Error: could not read resource usage for '{cluster_name}' — "
                f"the monitoring system {problem}. The cluster itself may be "
                "fine: get_dask_worker_count and get_dask_cluster_info do not "
                "depend on this data."
            )

        cpu_vals = [v for _, v in cpu_rows]
        mem_vals = [v for _, v in mem_rows]
        n = max(len(cpu_vals), len(mem_vals))
        if n == 0:
            return (
                f"No Running worker pods with usage metrics for '{cluster_name}'. "
                "The cluster may have zero workers, or metrics are not scraped yet."
            )

        lines = [
            f"# Resource usage for {cluster_name}",
            f"running workers sampled: {n}",
        ]
        cpu_stats = _stats(cpu_vals)
        if cpu_stats:
            cmin, cmax, cavg = cpu_stats
            lines += [
                "CPU (cores):",
                f"  min={cmin:.3f}  max={cmax:.3f}  avg={cavg:.3f}  "
                f"total={sum(cpu_vals):.3f}",
            ]
        else:
            lines.append("CPU (cores): no data")

        mem_stats = _stats(mem_vals)
        if mem_stats:
            mmin, mmax, mavg = mem_stats
            to_gib = 1024**3
            lines += [
                "Memory (GiB):",
                f"  min={mmin / to_gib:.2f}  max={mmax / to_gib:.2f}  "
                f"avg={mavg / to_gib:.2f}  total={sum(mem_vals) / to_gib:.2f}",
            ]
        else:
            lines.append("Memory (GiB): no data")

        return "\n".join(lines)

    @mcp.tool()
    async def scale_dask_cluster(cluster_name: str, n_workers: int) -> str:
        """Scale a Dask cluster to the requested number of workers.

        A cluster that is still starting cannot take workers, so this waits for
        its scheduler to reach RUNNING (bounded) before scaling —
        call it straight after create_dask_cluster without polling first. The
        scale itself is asynchronous: workers appear over the following seconds.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
            n_workers: Target worker count (0–200).
        """
        _validate_cluster_name(cluster_name)
        if n_workers < 0:
            raise UserError("Error: n_workers must be ≥ 0.")
        if n_workers > MAX_WORKERS:
            raise UserError(f"Error: n_workers must be ≤ {MAX_WORKERS}.")
        username = require_user()["username"]

        # A starting cluster rejects scaling; a RUNNING one costs one extra GET here.
        status, waited = await _await_scheduler(cluster_name, username)
        if status != "RUNNING":
            return (
                f"No scale request was sent: cluster '{cluster_name}' was still "
                f"{status} after {waited:.0f} s, and the gateway only accepts "
                "workers once the cluster is RUNNING.\n"
                "Next: get_dask_cluster_info for the current status, or "
                "query_dask_logs if it stays pending — the scheduler pod may "
                "be waiting for capacity. Calling scale_dask_cluster again "
                "resumes the wait."
            )

        await _gateway(
            "POST",
            f"/api/v1/clusters/{cluster_name}/scale",
            username=username,
            action=f"scale to {n_workers} worker(s)",
            cluster_name=cluster_name,
            ok=(200, 204),
            json={"count": n_workers},
        )
        lines = [f"Cluster '{cluster_name}' scaling to {n_workers} worker(s)."]
        if waited >= SCHEDULER_POLL_INTERVAL:
            lines.insert(0, f"Scheduler became RUNNING after {waited:.0f} s.")
        if n_workers:
            lines += _WORKERS_PENDING_NEXT
        return "\n".join(lines)

    @mcp.tool()
    async def stop_dask_cluster(cluster_name: str) -> str:
        """Stop and delete a Dask cluster, releasing all its resources.

        This is irreversible — running computations will be lost.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
        """
        _validate_cluster_name(cluster_name)
        username = require_user()["username"]
        resp = await _gateway(
            "DELETE",
            f"/api/v1/clusters/{cluster_name}",
            username=username,
            action=f"stop cluster '{cluster_name}'",
            cluster_name=cluster_name,
            ok=(200, 204, 404),
        )
        if resp.status_code == 404:
            return f"Cluster '{cluster_name}' not found (may have already stopped)."
        return f"Cluster '{cluster_name}' stopped."
