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
from typing import Literal, Optional

import httpx
from context import current_user
from mcp.server.fastmcp import Context
from metrics import instrumented_transport
from pydantic import BaseModel, Field

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


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=instrumented_transport(_gateway_target))


def _prom_client(target: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=instrumented_transport(target))


def _resolve_gateway(name: str) -> tuple[str, str]:
    """Return (canonical_name, url) or raise ValueError."""
    key = name.lower()
    if key not in _GATEWAYS:
        raise ValueError(f"Unknown gateway '{name}'. Valid options: {_GATEWAY_LIST}")
    return key, _GATEWAYS[key]


def _auth(username: str) -> dict:
    """HTTP Basic for SimpleAuthenticator (password field ignored when unset)."""
    cred = base64.b64encode(f"{username}:".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


def _cluster_id(cluster_name: str) -> str:
    """Strip the namespace prefix from a gateway cluster name.

    ``cms.ec5c698a…`` → ``ec5c698a…`` (matches dask-scheduler-/dask-worker- pods).
    """
    return cluster_name.rsplit(".", 1)[-1]


def _parse_clusters(payload) -> list[dict]:
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
        return gateway, f"unreachable ({exc})"
    if resp.status_code in (401, 403):
        return gateway, "not authorised (no access to this backend)"
    if resp.status_code != 200:
        return gateway, f"HTTP {resp.status_code}"
    return gateway, _parse_clusters(resp.json())


async def _require_owned_cluster(
    client: httpx.AsyncClient, url: str, username: str, cluster_name: str, gateway: str
) -> Optional[str]:
    """Return an error string if the user cannot access ``cluster_name``, else None."""
    try:
        resp = await client.get(
            f"{url}/api/v1/clusters/{cluster_name}",
            headers=_auth(username),
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        return f"Error: gateway '{gateway}' unreachable — {exc}"
    if resp.status_code == 404:
        return f"Cluster '{cluster_name}' not found on gateway '{gateway}'."
    if resp.status_code in (401, 403):
        return f"Error: not authorised to access cluster '{cluster_name}'."
    if resp.status_code != 200:
        return f"Error: HTTP {resp.status_code} — {resp.text[:300]}"
    return None


async def _prom_scalar(
    client: httpx.AsyncClient, base_url: str, query: str
) -> Optional[float]:
    try:
        resp = await client.get(
            f"{base_url}/api/v1/query", params={"query": query}, timeout=8.0
        )
    except httpx.RequestError:
        return None
    if resp.status_code != 200:
        return None
    results = resp.json().get("data", {}).get("result", [])
    if not results:
        return None
    try:
        return float(results[0]["value"][1])
    except (KeyError, IndexError, ValueError):
        return None


async def _prom_vector(
    client: httpx.AsyncClient, base_url: str, query: str
) -> list[tuple[dict, float]]:
    try:
        resp = await client.get(
            f"{base_url}/api/v1/query", params={"query": query}, timeout=8.0
        )
    except httpx.RequestError:
        return []
    if resp.status_code != 200:
        return []
    out: list[tuple[dict, float]] = []
    for row in resp.json().get("data", {}).get("result", []):
        try:
            out.append((row.get("metric") or {}, float(row["value"][1])))
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return out


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
) -> dict | str:
    """Validate create args and return the Gateway ``cluster_options`` body."""
    pixi = (pixi_project or "").strip()
    conda = (conda_env or "").strip()
    if pixi and conda:
        return (
            "Error: pixi_project and conda_env are mutually exclusive — "
            "specify only one."
        )
    if not pixi and not conda:
        return (
            "Error: provide either pixi_project (directory with pixi.toml) "
            "or conda_env (path to a conda/pixi env prefix)."
        )
    if worker_cores <= 0:
        return "Error: worker_cores must be > 0."
    if worker_memory <= 0:
        return "Error: worker_memory must be > 0 (GiB)."

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

    gateway: Literal["k8s", "slurm"] = Field(
        default="k8s",
        description=(
            "k8s = Geddes Kubernetes (workers see /work and /depot); "
            "slurm = Hammer Slurm (workers see /depot only)."
        ),
    )


class _EnvChoice(BaseModel):
    """Which worker environment to use."""

    env_source: Literal["global", "pixi", "conda"] = Field(
        default="global",
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

    size: Literal["default", "custom"] = Field(
        default="default",
        description=(
            f"default = {DEFAULT_WORKER_CORES:g} core / "
            f"{DEFAULT_WORKER_MEMORY:g} GiB per worker; "
            "custom = specify your own cores and memory."
        ),
    )


class _CustomSize(BaseModel):
    """Custom per-worker resources."""

    worker_cores: float = Field(
        gt=0, description="Cores per worker (k8s ≤ 64, Slurm ≤ 16)."
    )
    worker_memory: float = Field(gt=0, description="Memory per worker in GiB (≤ 64).")


class _CountChoice(BaseModel):
    """How many workers to start the cluster with."""

    count: Literal["0", "10", "50", "custom"] = Field(
        default="0",
        description=(
            "Number of workers to start with: 0 (scale later), 10, 50, or "
            "custom to enter your own number."
        ),
    )


class _CustomCount(BaseModel):
    """Custom starting worker count."""

    n_workers: int = Field(ge=0, description="Number of workers to start with.")


# Fallback shown when the client cannot render elicitation forms. Doubles as
# guidance for the create_cluster prompt so the agent asks in plain chat.
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


def register(mcp) -> None:
    @mcp.tool()
    async def list_dask_clusters() -> str:
        """List all running Dask clusters across every gateway backend.

        Queries the Kubernetes (k8s) and Slurm (Hammer) gateways concurrently and
        labels each cluster with its source backend. Results are scoped to the
        calling user only.
        """
        user = current_user.get()
        username = user["username"]

        async with _client() as client:
            results = await asyncio.gather(
                *[
                    _fetch_clusters(client, gw, url, username)
                    for gw, url in _GATEWAYS.items()
                ]
            )

        sections: list[str] = []
        total = 0
        for gateway, data in results:
            if isinstance(data, str):
                # error string — only surface if it isn't the "not authorised" case
                if "not authorised" not in data:
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
            return "No running Dask clusters on any gateway."

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
        user = current_user.get()
        try:
            gateway, url = _resolve_gateway(gateway)
        except ValueError as e:
            return str(e)

        async with _client() as client:
            try:
                resp = await client.get(
                    f"{url}/api/v1/options",
                    headers=_auth(user["username"]),
                    timeout=10.0,
                )
            except httpx.RequestError as exc:
                return f"Error: gateway '{gateway}' unreachable — {exc}"

        if resp.status_code in (401, 403):
            return f"Error: not authorised for gateway '{gateway}'."
        if resp.status_code != 200:
            return f"Error: HTTP {resp.status_code} — {resp.text[:300]}"

        fields = resp.json().get("cluster_options") or []
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
        environment → worker size → worker count. Clients that don't support
        elicitation get a short instruction listing the choices — collect them
        from the user and call again with explicit args.

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
            worker_cores: Cores per worker (k8s ≤ 64, Slurm ≤ 16). Defaults to
                          1 if the user picks the default size.
            worker_memory: Memory per worker in GiB (≤ 64). Defaults to 4 if the
                           user picks the default size.
            n_workers: Workers to start with (≥ 0). 0 (or omitted with a
                       non-eliciting client) starts the cluster empty.
            env: Extra environment variables for workers (e.g. X509_USER_PROXY,
                 PYTHONPATH, NB_UID/NB_GID for CERN/FNAL users).
        """
        if n_workers is not None and n_workers < 0:
            return "Error: n_workers must be ≥ 0."
        if worker_cores is not None and worker_cores <= 0:
            return "Error: worker_cores must be > 0."
        if worker_memory is not None and worker_memory <= 0:
            return "Error: worker_memory must be > 0 (GiB)."

        # ── Backend: ask the user if not supplied ──
        if gateway is None:
            status, data = await _elicit(
                ctx, "Choose the compute backend for your Dask cluster.", _BackendChoice
            )
            if status == "unsupported":
                return _CREATE_CHOICES_HELP
            if status != "accept":
                return "Cluster creation cancelled."
            gateway = data.gateway

        try:
            gateway, url = _resolve_gateway(gateway)
        except ValueError as e:
            return str(e)

        user = current_user.get()
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
            if status == "unsupported":
                return _CREATE_CHOICES_HELP
            if status != "accept":
                return "Cluster creation cancelled."
            env_source = data.env_source

        if env_source == "global":
            if gateway == "slurm":
                return (
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
                if status == "unsupported":
                    return _CREATE_CHOICES_HELP
                if status != "accept":
                    return "Cluster creation cancelled."
                pixi_project = data.pixi_project
                pixi_env = data.pixi_env
        elif env_source == "conda":
            if not conda_env:
                status, data = await _elicit(
                    ctx, "Provide the path to your conda environment.", _CondaChoice
                )
                if status == "unsupported":
                    return _CREATE_CHOICES_HELP
                if status != "accept":
                    return "Cluster creation cancelled."
                conda_env = data.conda_env
        else:
            return (
                f"Error: unknown env_source '{env_source}'. "
                "Use 'global', 'pixi', or 'conda'."
            )

        # ── Worker size: ask only if neither cores nor memory was supplied ──
        if worker_cores is None and worker_memory is None:
            status, data = await _elicit(ctx, "Choose the worker size.", _SizeChoice)
            if status == "unsupported":
                return _CREATE_CHOICES_HELP
            if status != "accept":
                return "Cluster creation cancelled."
            if data.size == "custom":
                status, size = await _elicit(
                    ctx, "Specify the resources per worker.", _CustomSize
                )
                if status == "unsupported":
                    return _CREATE_CHOICES_HELP
                if status != "accept":
                    return "Cluster creation cancelled."
                worker_cores = size.worker_cores
                worker_memory = size.worker_memory
        if worker_cores is None:
            worker_cores = DEFAULT_WORKER_CORES
        if worker_memory is None:
            worker_memory = DEFAULT_WORKER_MEMORY

        # ── Worker count: ask only if n_workers was not supplied ──
        if n_workers is None:
            status, data = await _elicit(
                ctx, "How many workers should the cluster start with?", _CountChoice
            )
            if status == "unsupported":
                return _CREATE_CHOICES_HELP
            if status != "accept":
                return "Cluster creation cancelled."
            if data.count == "custom":
                status, count = await _elicit(
                    ctx, "Specify the number of workers to start with.", _CustomCount
                )
                if status == "unsupported":
                    return _CREATE_CHOICES_HELP
                if status != "accept":
                    return "Cluster creation cancelled."
                n_workers = count.n_workers
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
        if isinstance(options, str):
            return options

        async with _client() as client:
            try:
                resp = await client.post(
                    f"{url}/api/v1/clusters/",
                    headers=_auth(username),
                    json={"cluster_options": options},
                    timeout=60.0,
                )
            except httpx.RequestError as exc:
                return f"Error: gateway '{gateway}' unreachable — {exc}"

            if resp.status_code == 422:
                reason = resp.reason_phrase or ""
                try:
                    reason = resp.json().get("message") or resp.text[:400]
                except Exception:
                    reason = resp.text[:400] or reason
                return f"Error: gateway rejected create — {reason}"
            if resp.status_code not in (200, 201):
                return f"Error: HTTP {resp.status_code} — {resp.text[:400]}"

            cluster_name = resp.json().get("name", "")
            if not cluster_name:
                return f"Error: create succeeded but no cluster name in response — {resp.text[:300]}"

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
                    f"Created, but scale failed (gateway unreachable): {exc}",
                    "Retry with scale_dask_cluster.",
                ]
                return "\n".join(lines)

            if scale.status_code not in (200, 204):
                lines += [
                    "",
                    f"Created, but scale returned HTTP {scale.status_code}: "
                    f"{scale.text[:300]}",
                    "Retry with scale_dask_cluster.",
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
        user = current_user.get()
        try:
            _, url = _resolve_gateway(gateway)
        except ValueError as e:
            return str(e)

        async with _client() as client:
            try:
                resp = await client.get(
                    f"{url}/api/v1/clusters/{cluster_name}",
                    headers=_auth(user["username"]),
                    timeout=10.0,
                )
            except httpx.RequestError as exc:
                return f"Error: gateway '{gateway}' unreachable — {exc}"

        if resp.status_code == 404:
            return f"Cluster '{cluster_name}' not found on gateway '{gateway}'."
        if resp.status_code != 200:
            return f"Error: HTTP {resp.status_code} — {resp.text[:300]}"

        c = resp.json()
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
        user = current_user.get()
        username = user["username"]
        try:
            _, url = _resolve_gateway(gateway)
        except ValueError as e:
            return str(e)

        async with _client() as gw_client:
            err = await _require_owned_cluster(
                gw_client, url, username, cluster_name, gateway
            )
            if err:
                return err

        cid = _cluster_id(cluster_name)
        sched_pod = f"dask-scheduler-{cid}"
        total_q = f'sum(dask_scheduler_workers{{user="{username}",pod="{sched_pod}"}})'
        by_state_q = (
            f"sum by (state) ("
            f'dask_scheduler_workers{{user="{username}",pod="{sched_pod}"}})'
        )
        desired_q = f'sum(dask_scheduler_desired_workers{{user="{username}",pod="{sched_pod}"}})'

        async with _prom_client("prometheus") as prom:
            total, by_state, desired = await asyncio.gather(
                _prom_scalar(prom, PROMETHEUS_URL, total_q),
                _prom_vector(prom, PROMETHEUS_URL, by_state_q),
                _prom_scalar(prom, PROMETHEUS_URL, desired_q),
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
        user = current_user.get()
        username = user["username"]
        try:
            _, url = _resolve_gateway(gateway)
        except ValueError as e:
            return str(e)

        async with _client() as gw_client:
            err = await _require_owned_cluster(
                gw_client, url, username, cluster_name, gateway
            )
            if err:
                return err

        cid = _cluster_id(cluster_name)
        worker_re = f"dask-worker-{cid}-.+"
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

        async with _prom_client("cluster-prometheus") as prom:
            cpu_rows, mem_rows = await asyncio.gather(
                _prom_vector(prom, CLUSTER_PROMETHEUS_URL, cpu_q),
                _prom_vector(prom, CLUSTER_PROMETHEUS_URL, mem_q),
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
            n_workers: Target worker count (≥ 0).
            gateway: Gateway backend — 'k8s' (default) or 'slurm'.
        """
        if n_workers < 0:
            return "Error: n_workers must be ≥ 0."

        user = current_user.get()
        try:
            _, url = _resolve_gateway(gateway)
        except ValueError as e:
            return str(e)

        async with _client() as client:
            try:
                resp = await client.post(
                    f"{url}/api/v1/clusters/{cluster_name}/scale",
                    headers=_auth(user["username"]),
                    json={"count": n_workers},
                    timeout=10.0,
                )
            except httpx.RequestError as exc:
                return f"Error: gateway '{gateway}' unreachable — {exc}"

        if resp.status_code == 404:
            return f"Cluster '{cluster_name}' not found on gateway '{gateway}'."
        if resp.status_code not in (200, 204):
            return f"Error: HTTP {resp.status_code} — {resp.text[:300]}"

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
        user = current_user.get()
        try:
            _, url = _resolve_gateway(gateway)
        except ValueError as e:
            return str(e)

        async with _client() as client:
            try:
                resp = await client.delete(
                    f"{url}/api/v1/clusters/{cluster_name}",
                    headers=_auth(user["username"]),
                    timeout=10.0,
                )
            except httpx.RequestError as exc:
                return f"Error: gateway '{gateway}' unreachable — {exc}"

        if resp.status_code == 404:
            return f"Cluster '{cluster_name}' not found on gateway '{gateway}' (may have already stopped)."
        if resp.status_code not in (200, 204):
            return f"Error: HTTP {resp.status_code} — {resp.text[:300]}"

        return f"Cluster '{cluster_name}' on '{gateway}' stopped."
