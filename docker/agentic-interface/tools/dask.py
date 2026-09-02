"""Dask cluster tools — create, list, inspect, scale, stop via Gateway API.

Two gateway backends are supported: ``k8s`` (Geddes Kubernetes) and ``slurm``
(Hammer Slurm). list_dask_clusters queries both; other tools take a `gateway`
argument so the correct backend is targeted.

Gateways here use SimpleAuthenticator (password ignored). Calls authenticate as
the Hub username via HTTP Basic so each user only sees their own clusters.

Worker counts come from the AF Prometheus (dask_scheduler_workers). Live CPU /
memory usage comes from the cluster Prometheus (cadvisor), filtered to Running
worker pods.
"""

import asyncio
import base64
import os
import re
from typing import Any, Optional

import httpx
from context import require_user
from errors import (
    AuthError,
    Failure,
    UpstreamError,
    UserError,
    describe_exception,
    http_error,
    json_body,
    malformed_response,
    response_detail,
    unreachable,
)
from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field
from shared import prom_query, prom_scalar, prom_vector, quote_label, shared_client

from tools.elicitation import elicit as _elicit

# Gateway name → internal k8s service URL.
# Override individual entries via env vars if needed.
_GATEWAYS: dict[str, str] = {
    "k8s": os.environ.get(
        "DASK_GATEWAY_K8S_URL",
        "http://api-dask-gateway-k8s.cms.svc.cluster.local:8000",
    ),
    "slurm": os.environ.get(
        "DASK_GATEWAY_SLURM_URL",
        "http://api-dask-gateway-k8s-slurm.cms.svc.cluster.local:8000",
    ),
}
_GATEWAY_LIST = ", ".join(_GATEWAYS)

# Shared pixi env pre-built for everyone. Lives on /work, so it is only usable
# by Kubernetes workers — Slurm (Hammer) workers can see /depot but not /work.
GLOBAL_PIXI_PROJECT = os.environ.get("DASK_GLOBAL_PIXI_PROJECT", "/work/pixi/global")

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-server:9090")
# cadvisor / kubelet metrics (container_*) live on the Rancher Prometheus, not
# the AF prometheus-server that scrapes dask-gateway-monitor.
CLUSTER_PROMETHEUS_URL = os.environ.get(
    "CLUSTER_PROMETHEUS_URL",
    "http://rancher-monitoring-prometheus.cattle-monitoring-system.svc.cluster.local:9090",
)

# Upstream-metrics target labels, derived per request so one client can talk
# to several gateway backends and still be broken down by backend.
_HOST_TO_GATEWAY = {httpx.URL(url).host: gw for gw, url in _GATEWAYS.items()}


def _gateway_target(request: httpx.Request) -> str:
    return f"dask-gateway-{_HOST_TO_GATEWAY.get(request.url.host, 'unknown')}"


# Pooled process-wide clients (see shared.py) — never close these at call sites.
def _client() -> httpx.AsyncClient:
    return shared_client("dask-gateway", target=_gateway_target)


def _prom_client(target: str) -> httpx.AsyncClient:
    return shared_client(target)


# Limits mirrored from the gateway configs — keep in sync with
# apps/dask-gateway/dask-gateway-k8s/values.yaml (cluster_max_workers = 200,
# Float worker_cores/worker_memory 0.1–64) and
# apps/dask-gateway/dask-gateway-k8s-slurm/values.yaml (Integer worker_cores
# 1–16, Float worker_memory 1–64; it sets no cluster_max_workers, so the k8s
# ceiling doubles as the sanity bound there).
MAX_WORKERS = 200
_WORKER_LIMITS: dict[str, dict] = {
    "k8s": {"cores": (0.1, 64.0), "memory": (0.1, 64.0), "integer_cores": False},
    "slurm": {"cores": (1.0, 16.0), "memory": (1.0, 64.0), "integer_cores": True},
}


def _check_worker_size(gateway: str, worker_cores: float, worker_memory: float) -> None:
    """Raise UserError if the per-worker size exceeds the gateway's configured
    option limits. The gateway enforces these too; checking here turns a 422
    round-trip into an immediate, precise message."""
    lim = _WORKER_LIMITS[gateway]
    lo, hi = lim["cores"]
    if not lo <= worker_cores <= hi:
        raise UserError(
            f"Error: worker_cores must be between {lo:g} and {hi:g} "
            f"on gateway '{gateway}'."
        )
    if lim["integer_cores"] and worker_cores != int(worker_cores):
        raise UserError(
            f"Error: gateway '{gateway}' requires a whole number of worker_cores."
        )
    lo, hi = lim["memory"]
    if not lo <= worker_memory <= hi:
        raise UserError(
            f"Error: worker_memory must be between {lo:g} and {hi:g} GiB "
            f"on gateway '{gateway}'."
        )


# Cluster names land in gateway URL paths and (via _cluster_id) in PromQL
# regexes, so only accept the character set gateways actually emit.
_CLUSTER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_cluster_name(cluster_name: str) -> None:
    """Raise UserError if ``cluster_name`` is not a safe name."""
    if not _CLUSTER_NAME_RE.match(cluster_name or ""):
        raise UserError(
            f"Error: invalid cluster name {cluster_name!r} — use a name "
            "returned by list_dask_clusters."
        )


def _resolve_gateway(name: str) -> tuple[str, str]:
    """Return (canonical_name, url) or raise UserError."""
    key = name.lower()
    if key not in _GATEWAYS:
        raise UserError(f"Unknown gateway '{name}'. Valid options: {_GATEWAY_LIST}")
    return key, _GATEWAYS[key]


def _auth(username: str) -> dict:
    """HTTP Basic for SimpleAuthenticator (password field ignored when unset)."""
    cred = base64.b64encode(f"{username}:".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


# ── failure reporting ─────────────────────────────────────────────────────────


def _gateway_unreachable(gateway: str, exc: BaseException) -> UpstreamError:
    return unreachable(f"gateway '{gateway}'", exc)


def _gateway_http_error(
    gateway: str,
    resp: httpx.Response,
    action: str,
    cluster_name: Optional[str] = None,
) -> Failure:
    """What a non-2xx gateway answer means for this user.

    Gateways authenticate by Hub username, so a refusal is about access to
    that backend, not about the token; a 404 is about the cluster name.
    """
    code = resp.status_code
    if code in (401, 403):
        access = (
            "Slurm (Hammer) clusters need an account on Hammer; "
            if gateway == "slurm"
            else ""
        )
        return AuthError(
            f"Error: not authorised on gateway '{gateway}' to {action} "
            f"(HTTP {code}). {access}if you believe you should have access, "
            "contact AF support."
        )
    if code == 404 and cluster_name:
        return UserError(
            f"Cluster '{cluster_name}' not found on gateway '{gateway}'. Call "
            "list_dask_clusters for the current names — and check the gateway "
            "argument, as names are per backend."
        )
    if code in (409, 422):
        return UserError(
            f"Error: gateway '{gateway}' rejected the request to {action} — "
            f"{response_detail(resp, limit=400) or 'no reason given'}."
        )
    return http_error(f"gateway '{gateway}'", resp, action=action)


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


def _fmt_cluster(c: dict, gateway: str) -> str:
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
    lines = [f"**{name}**  gateway={gateway}  status={status}{scale_info}"]
    if scheduler:
        lines.append(f"  scheduler: {scheduler}")
    return "\n".join(lines)


async def _fetch_clusters(
    client: httpx.AsyncClient, gateway: str, url: str, username: str
) -> tuple[str, list[dict] | str]:
    """Return (gateway_name, clusters_or_error_string)."""
    try:
        resp = await client.get(
            f"{url}/api/v1/clusters/", headers=_auth(username), timeout=10.0
        )
    except httpx.RequestError as exc:
        return gateway, f"unreachable ({describe_exception(exc)})"
    if resp.status_code in (401, 403):
        return gateway, "not authorised (no access to this backend)"
    if resp.status_code != 200:
        detail = response_detail(resp, limit=120)
        return gateway, f"HTTP {resp.status_code}" + (f" — {detail}" if detail else "")
    payload = json_body(resp)
    if payload is None:
        return gateway, "returned a malformed cluster list"
    return gateway, _parse_clusters(payload)


async def _require_owned_cluster(
    client: httpx.AsyncClient, url: str, username: str, cluster_name: str, gateway: str
) -> None:
    """Raise a Failure if the user cannot access ``cluster_name``."""
    try:
        resp = await client.get(
            f"{url}/api/v1/clusters/{cluster_name}",
            headers=_auth(username),
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise _gateway_unreachable(gateway, exc)
    if resp.status_code != 200:
        raise _gateway_http_error(
            gateway, resp, f"access cluster '{cluster_name}'", cluster_name
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
    """Build the env mapping required by gateway options handlers.

    Handlers always ``pop("PATH")`` and prepend the conda/pixi bin dir, so PATH
    must be present. Callers can override/extend via ``extra``.
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


class _BackendChoice(BaseModel):
    """Which compute backend to run the Dask workers on."""

    gateway: str = Field(
        default="k8s",
        json_schema_extra={"enum": ["k8s", "slurm"]},
        description=(
            "k8s = Geddes Kubernetes (workers see /work and /depot); "
            "slurm = Hammer Slurm (workers see /depot only)."
        ),
    )


class _EnvChoice(BaseModel):
    """Which worker environment to use."""

    env_source: str = Field(
        default="global",
        json_schema_extra={"enum": ["global", "pixi", "conda"]},
        description=(
            "global = shared pixi env at /work/pixi/global (k8s only); "
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
        gt=0, description="Cores per worker (k8s: 0.1–64; Slurm: whole numbers 1–16)."
    )
    worker_memory: float = Field(
        gt=0, description="Memory per worker in GiB (k8s: 0.1–64; Slurm: 1–64)."
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


# Returned whenever an interactive choice couldn't be collected — the client
# can't render elicitation, or the prompt came back declined/cancelled. Agent
# clients (Claude Code among them) may auto-decline elicitation without ever
# showing the user a form, so a non-accept never proves the user said no;
# rather than dead-ending, hand the agent everything it needs to ask in chat
# and retry. Doubles as guidance for the create_cluster prompt.
_CREATE_CHOICES_HELP = (
    "create_dask_cluster needs two choices from the user. Ask them (use the "
    "client's multiple-choice UI if available), then call create_dask_cluster "
    "again with explicit arguments:\n"
    "1) gateway: 'k8s' (Geddes Kubernetes) or 'slurm' (Hammer).\n"
    "2) worker environment — one of:\n"
    "   • global (default): shared pixi env at /work/pixi/global — pass "
    "env_source='global' (k8s only; Slurm cannot see /work).\n"
    "   • your pixi project: pass pixi_project='/path' (+ optional pixi_env).\n"
    "   • your conda env: pass conda_env='/path' (use /depot for Slurm).\n"
    "3) worker size: default (1 core / 4 GiB) or custom (pass worker_cores + "
    "worker_memory in GiB).\n"
    "4) worker count to start with: 0, 10, 50, or a custom number (pass "
    "n_workers)."
)


def register(mcp: Any) -> None:
    @mcp.tool()
    async def list_dask_clusters() -> str:
        """List all running Dask clusters across every gateway backend.

        Queries the Kubernetes (k8s) and Slurm (Hammer) gateways concurrently and
        labels each cluster with its source backend. Results are scoped to the
        calling user only.
        """
        user = require_user()
        username = user["username"]

        client = _client()
        results = await asyncio.gather(
            *[
                _fetch_clusters(client, gw, url, username)
                for gw, url in _GATEWAYS.items()
            ]
        )

        sections: list[str] = []
        refused: list[str] = []
        total = 0
        for gateway, data in results:
            if isinstance(data, str):
                # A backend the user has no access to is not an error for
                # most users (few have Hammer accounts) — but if nothing at
                # all can be listed it must not read as "no clusters".
                if "not authorised" in data:
                    refused.append(gateway)
                else:
                    sections.append(f"[{gateway}] error: {data}")
                continue
            if not data:
                continue
            total += len(data)
            sections.append(
                f"### {gateway} ({len(data)} cluster(s))\n"
                + "\n\n".join(_fmt_cluster(c, gateway) for c in data)
            )

        if not sections:
            if refused and len(refused) == len(results):
                raise AuthError(
                    f"Error: not authorised on any gateway ({', '.join(refused)}) "
                    f"for user '{username}', so no clusters can be listed. "
                    "Contact AF support if you expect access."
                )
            note = (
                f" (gateway {', '.join(refused)}: not authorised — clusters "
                "there, if any, are not visible to you)"
                if refused
                else ""
            )
            return f"No running Dask clusters on any gateway{note}."

        header = f"# {total} Dask cluster(s) across all gateways\n"
        return header + "\n\n".join(sections)

    @mcp.tool()
    async def list_dask_cluster_options(gateway: str = "k8s") -> str:
        """List the create-time options accepted by a Dask Gateway backend.

        Call this before create_dask_cluster to see field names, defaults, and
        limits for the chosen gateway (Kubernetes vs Slurm differ slightly).

        Args:
            gateway: 'k8s' (Geddes Kubernetes) or 'slurm' (Hammer Slurm).
        """
        user = require_user()
        gateway, url = _resolve_gateway(gateway)

        try:
            resp = await _client().get(
                f"{url}/api/v1/options",
                headers=_auth(user["username"]),
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            raise _gateway_unreachable(gateway, exc)

        if resp.status_code != 200:
            raise _gateway_http_error(gateway, resp, "list cluster options")

        payload = json_body(resp)
        if not isinstance(payload, dict):
            raise malformed_response(
                f"gateway '{gateway}'", resp, "a cluster-options document"
            )
        fields = payload.get("cluster_options") or []
        backend = "Kubernetes (Geddes)" if gateway == "k8s" else "Slurm (Hammer)"
        lines = [
            f"# Cluster options for gateway={gateway} — {backend}",
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
            "  • k8s workers see /work; Slurm (Hammer) workers do not — "
            "put pixi/conda envs on /depot for Slurm.",
            "  • Only one active cluster per user is allowed.",
        ]
        return "\n".join(lines)

    @mcp.tool()
    async def create_dask_cluster(
        ctx: Context,
        gateway: Optional[str] = None,
        env_source: Optional[str] = None,
        pixi_project: Optional[str] = None,
        pixi_env: str = "default",
        conda_env: Optional[str] = None,
        worker_cores: Optional[float] = None,
        worker_memory: Optional[float] = None,
        n_workers: Optional[int] = None,
        env: Optional[dict] = None,
    ) -> str:
        """Create a new Dask Gateway cluster on Kubernetes or Slurm.

        Any choice not supplied is asked interactively via the client's
        multiple-choice UI (MCP elicitation), one question at a time: backend →
        environment → worker size → worker count. If a choice can't be
        collected (the client doesn't support elicitation, or the prompt is
        declined or dismissed), a short instruction listing the choices is
        returned instead — collect them from the user and call again with
        explicit args.

        Backend (``gateway``):
          • 'k8s' — Geddes Kubernetes workers (can use /work and /depot)
          • 'slurm' — Hammer Slurm workers (use /depot for envs; no /work)

        Worker environment (``env_source``):
          • 'global' — shared pixi env at /work/pixi/global (k8s only)
          • 'pixi' — your own pixi project (set ``pixi_project`` + ``pixi_env``)
          • 'conda' — your own conda env (set ``conda_env``)

        Passing ``pixi_project`` or ``conda_env`` directly implies the matching
        ``env_source``. Passing ``worker_cores``/``worker_memory`` skips the size
        question; passing ``n_workers`` skips the count question.

        Args:
            gateway: 'k8s' or 'slurm'. Elicited from the user if omitted.
            env_source: 'global', 'pixi', or 'conda'. Elicited if omitted and no
                        pixi_project/conda_env is given.
            pixi_project: Path to a pixi project directory.
            pixi_env: Pixi environment name within the project (default 'default').
            conda_env: Path to a conda/mamba env prefix (mutually exclusive with
                       pixi_project).
            worker_cores: Cores per worker (k8s: 0.1–64; Slurm: whole numbers
                          1–16). Defaults to 1 if the user picks the default
                          size.
            worker_memory: Memory per worker in GiB (k8s: 0.1–64; Slurm: 1–64).
                           Defaults to 4 if the user picks the default size.
            n_workers: Workers to start with (0–200). 0 (or omitted with a
                       non-eliciting client) starts the cluster empty.
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

        # ── Backend: ask the user if not supplied ──
        if gateway is None:
            status, data = await _elicit(
                ctx, "Choose the compute backend for your Dask cluster.", _BackendChoice
            )
            if status != "accept":
                return _CREATE_CHOICES_HELP
            gateway = data.gateway

        gateway, url = _resolve_gateway(gateway)

        user = require_user()
        username = user["username"]

        # ── Worker environment: infer from explicit paths, else ask the user ──
        if pixi_project:
            env_source = "pixi"
        elif conda_env:
            env_source = "conda"
        elif env_source is None:
            status, data = await _elicit(
                ctx, "Choose the worker environment.", _EnvChoice
            )
            if status != "accept":
                return _CREATE_CHOICES_HELP
            env_source = data.env_source

        if env_source == "global":
            if gateway == "slurm":
                raise UserError(
                    "Error: the global pixi env lives on /work, which Slurm "
                    "(Hammer) workers cannot access. Choose a pixi project or "
                    "conda env on /depot instead."
                )
            pixi_project = GLOBAL_PIXI_PROJECT
            pixi_env = "default"
        elif env_source == "pixi":
            if not pixi_project:
                status, data = await _elicit(
                    ctx, "Provide the path to your pixi project.", _PixiChoice
                )
                if status != "accept":
                    return _CREATE_CHOICES_HELP
                pixi_project = data.pixi_project
                pixi_env = data.pixi_env
        elif env_source == "conda":
            if not conda_env:
                status, data = await _elicit(
                    ctx, "Provide the path to your conda environment.", _CondaChoice
                )
                if status != "accept":
                    return _CREATE_CHOICES_HELP
                conda_env = data.conda_env
        else:
            raise UserError(
                f"Error: unknown env_source '{env_source}'. "
                "Use 'global', 'pixi', or 'conda'."
            )

        # ── Worker size: ask only if neither cores nor memory was supplied ──
        if worker_cores is None and worker_memory is None:
            status, data = await _elicit(ctx, "Choose the worker size.", _SizeChoice)
            if status != "accept":
                return _CREATE_CHOICES_HELP
            if data.size == "custom":
                status, size = await _elicit(
                    ctx, "Specify the resources per worker.", _CustomSize
                )
                if status != "accept":
                    return _CREATE_CHOICES_HELP
                worker_cores = size.worker_cores
                worker_memory = size.worker_memory
        if worker_cores is None:
            worker_cores = DEFAULT_WORKER_CORES
        if worker_memory is None:
            worker_memory = DEFAULT_WORKER_MEMORY
        _check_worker_size(gateway, worker_cores, worker_memory)

        # ── Worker count: ask only if n_workers was not supplied ──
        if n_workers is None:
            status, data = await _elicit(
                ctx, "How many workers should the cluster start with?", _CountChoice
            )
            if status != "accept":
                return _CREATE_CHOICES_HELP
            if data.count == "custom":
                status, count = await _elicit(
                    ctx, "Specify the number of workers to start with.", _CustomCount
                )
                if status != "accept":
                    return _CREATE_CHOICES_HELP
                n_workers = count.n_workers
                if n_workers > MAX_WORKERS:
                    raise UserError(f"Error: n_workers must be ≤ {MAX_WORKERS}.")
            else:
                n_workers = int(data.count)

        options = _build_cluster_options(
            username=username,
            pixi_project=pixi_project,
            pixi_env=pixi_env,
            conda_env=conda_env,
            worker_cores=worker_cores,
            worker_memory=worker_memory,
            env=env,
        )

        client = _client()
        try:
            resp = await client.post(
                f"{url}/api/v1/clusters/",
                headers=_auth(username),
                json={"cluster_options": options},
                timeout=60.0,
            )
        except httpx.RequestError as exc:
            raise unreachable(
                f"gateway '{gateway}'",
                exc,
                next_step="No cluster was created. Try again in a minute; if the "
                "gateway stays unreachable, get_facility_health shows whether "
                "scale-out is degraded.",
            )

        if resp.status_code not in (200, 201):
            raise _gateway_http_error(gateway, resp, "create the cluster")

        payload = json_body(resp)
        cluster_name = payload.get("name", "") if isinstance(payload, dict) else ""
        if not cluster_name:
            malformed = malformed_response(
                f"gateway '{gateway}'", resp, "a cluster record with a name"
            )
            raise UpstreamError(
                f"{malformed} The cluster may have been created anyway — check "
                "list_dask_clusters before creating another."
            )

        lines = [
            f"Cluster '{cluster_name}' created on gateway '{gateway}'.",
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

        try:
            scale = await client.post(
                f"{url}/api/v1/clusters/{cluster_name}/scale",
                headers=_auth(username),
                json={"count": n_workers},
                timeout=30.0,
            )
        except httpx.RequestError as exc:
            lines += [
                "",
                "Created with 0 workers — the scale request failed: gateway "
                f"'{gateway}' unreachable — {describe_exception(exc)}.",
                f"Retry with scale_dask_cluster('{cluster_name}', {n_workers}, "
                f"gateway='{gateway}').",
            ]
            return "\n".join(lines)

        if scale.status_code not in (200, 204):
            lines += [
                "",
                "Created with 0 workers — the scale request failed: "
                + str(
                    _gateway_http_error(
                        gateway, scale, f"scale to {n_workers} worker(s)", cluster_name
                    )
                ),
                f"Retry with scale_dask_cluster('{cluster_name}', {n_workers}, "
                f"gateway='{gateway}').",
            ]
            return "\n".join(lines)

        lines.append(f"Scaling to {n_workers} worker(s).")
        lines.append(
            "Next: get_dask_worker_count / get_dask_cluster_info to confirm ready."
        )
        return "\n".join(lines)

    @mcp.tool()
    async def get_dask_cluster_info(cluster_name: str, gateway: str = "k8s") -> str:
        """Get detailed information about a specific Dask cluster.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
            gateway: Gateway backend — 'k8s' (default) or 'slurm'.
        """
        _validate_cluster_name(cluster_name)
        user = require_user()
        _, url = _resolve_gateway(gateway)

        try:
            resp = await _client().get(
                f"{url}/api/v1/clusters/{cluster_name}",
                headers=_auth(user["username"]),
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            raise _gateway_unreachable(gateway, exc)

        if resp.status_code != 200:
            raise _gateway_http_error(
                gateway, resp, f"inspect cluster '{cluster_name}'", cluster_name
            )

        c = json_body(resp)
        if not isinstance(c, dict):
            raise malformed_response(f"gateway '{gateway}'", resp, "a cluster record")
        workers = c.get("workers") or {}
        worker_lines: list[str] = []
        if isinstance(workers, dict):
            for wname, winfo in list(workers.items())[:20]:
                state = winfo.get("status", "?") if isinstance(winfo, dict) else "?"
                worker_lines.append(f"  {wname}: {state}")
            if len(workers) > 20:
                worker_lines.append(f"  … {len(workers) - 20} more")

        opts = c.get("options", {})
        sections = [_fmt_cluster(c, gateway)]
        if opts:
            sections.append(
                "Options:\n" + "\n".join(f"  {k}: {v}" for k, v in opts.items())
            )
        if worker_lines:
            sections.append(f"Workers ({len(workers)}):\n" + "\n".join(worker_lines))
        return "\n\n".join(sections)

    @mcp.tool()
    async def get_dask_worker_count(cluster_name: str, gateway: str = "k8s") -> str:
        """Return the current number of workers for a Dask cluster (by state).

        Uses the scheduler's Prometheus metrics. Prefer this over guessing from
        list_dask_clusters when you need an accurate live count.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
            gateway: Gateway backend — 'k8s' (default) or 'slurm'.
        """
        _validate_cluster_name(cluster_name)
        user = require_user()
        username = user["username"]
        _, url = _resolve_gateway(gateway)

        await _require_owned_cluster(_client(), url, username, cluster_name, gateway)

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

        prom = _prom_client("prometheus")
        (total, p1), (by_state, p2), (desired, p3) = await asyncio.gather(
            _prom_scalar(prom, PROMETHEUS_URL, total_q),
            _prom_vector(prom, PROMETHEUS_URL, by_state_q),
            _prom_scalar(prom, PROMETHEUS_URL, desired_q),
        )

        problem = p1 or p2 or p3
        if problem:
            raise UpstreamError(
                f"Error: could not read worker metrics for '{cluster_name}' — "
                f"Prometheus {problem}. The cluster itself may be fine: get_dask_cluster_info "
                "shows the gateway's own view of its workers."
            )
        if total is None:
            return (
                f"No worker metrics for cluster '{cluster_name}' "
                "(scheduler may still be starting, or metrics are stale)."
            )

        lines = [
            f"# Workers for {cluster_name} (gateway={gateway})",
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
    async def get_dask_cluster_usage(cluster_name: str, gateway: str = "k8s") -> str:
        """CPU and memory usage across Running workers of a Dask cluster.

        Reports per-worker min / max / average for CPU (cores) and memory (GiB),
        plus cluster totals. Scoped to the calling user's cluster.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
            gateway: Gateway backend — 'k8s' (default) or 'slurm'.
        """
        _validate_cluster_name(cluster_name)
        user = require_user()
        username = user["username"]
        _, url = _resolve_gateway(gateway)

        await _require_owned_cluster(_client(), url, username, cluster_name, gateway)

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

        prom = _prom_client("cluster-prometheus")
        (cpu_rows, p1), (mem_rows, p2) = await asyncio.gather(
            _prom_vector(prom, CLUSTER_PROMETHEUS_URL, cpu_q),
            _prom_vector(prom, CLUSTER_PROMETHEUS_URL, mem_q),
        )
        problem = p1 or p2
        if problem:
            raise UpstreamError(
                f"Error: could not read resource usage for '{cluster_name}' — "
                f"the monitoring system {problem}. The cluster itself may be fine: get_dask_worker_count "
                "and get_dask_cluster_info do not depend on this data."
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
            f"# Resource usage for {cluster_name} (gateway={gateway})",
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
    async def scale_dask_cluster(
        cluster_name: str, n_workers: int, gateway: str = "k8s"
    ) -> str:
        """Scale a Dask cluster to the requested number of workers.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
            n_workers: Target worker count (0–200).
            gateway: Gateway backend — 'k8s' (default) or 'slurm'.
        """
        _validate_cluster_name(cluster_name)
        if n_workers < 0:
            raise UserError("Error: n_workers must be ≥ 0.")
        if n_workers > MAX_WORKERS:
            raise UserError(f"Error: n_workers must be ≤ {MAX_WORKERS}.")

        user = require_user()
        _, url = _resolve_gateway(gateway)

        try:
            resp = await _client().post(
                f"{url}/api/v1/clusters/{cluster_name}/scale",
                headers=_auth(user["username"]),
                json={"count": n_workers},
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            raise _gateway_unreachable(gateway, exc)

        if resp.status_code not in (200, 204):
            raise _gateway_http_error(
                gateway, resp, f"scale to {n_workers} worker(s)", cluster_name
            )

        return (
            f"Cluster '{cluster_name}' on '{gateway}' scaling to {n_workers} worker(s)."
        )

    @mcp.tool()
    async def stop_dask_cluster(cluster_name: str, gateway: str = "k8s") -> str:
        """Stop and delete a Dask cluster, releasing all its resources.

        This is irreversible — running computations will be lost.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
            gateway: Gateway backend — 'k8s' (default) or 'slurm'.
        """
        _validate_cluster_name(cluster_name)
        user = require_user()
        _, url = _resolve_gateway(gateway)

        try:
            resp = await _client().delete(
                f"{url}/api/v1/clusters/{cluster_name}",
                headers=_auth(user["username"]),
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            raise _gateway_unreachable(gateway, exc)

        if resp.status_code == 404:
            return f"Cluster '{cluster_name}' not found on gateway '{gateway}' (may have already stopped)."
        if resp.status_code not in (200, 204):
            raise _gateway_http_error(
                gateway, resp, f"stop cluster '{cluster_name}'", cluster_name
            )

        return f"Cluster '{cluster_name}' on '{gateway}' stopped."
