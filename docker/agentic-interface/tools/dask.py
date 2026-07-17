"""Dask cluster tools — list, inspect, scale, stop via Dask Gateway REST API.

Multiple gateway backends are supported (k8s, slurm-hammer, slurm-gautschi, slurm).
list_dask_clusters queries all of them; the other tools take a `gateway` argument
so the correct backend is targeted.

Gateways here use SimpleAuthenticator (password ignored). Calls authenticate as
the Hub username via HTTP Basic so each user only sees their own clusters.
"""

import asyncio
import base64
import os

import httpx
from context import current_user
from metrics import instrumented_transport

# Gateway name → internal k8s service URL.
# Override individual entries via env vars if needed.
_GATEWAYS: dict[str, str] = {
    "k8s": os.environ.get(
        "DASK_GATEWAY_K8S_URL",
        "http://api-dask-gateway-k8s.cms.svc.cluster.local:8000",
    ),
    "slurm-hammer": os.environ.get(
        "DASK_GATEWAY_SLURM_HAMMER_URL",
        "http://api-dask-gateway-k8s-slurm-hammer.cms.svc.cluster.local:8000",
    ),
    "slurm-gautschi": os.environ.get(
        "DASK_GATEWAY_SLURM_GAUTSCHI_URL",
        "http://api-dask-gateway-k8s-slurm-gautschi.cms.svc.cluster.local:8000",
    ),
    "slurm": os.environ.get(
        "DASK_GATEWAY_SLURM_URL",
        "http://api-dask-gateway-k8s-slurm.cms.svc.cluster.local:8000",
    ),
}
_GATEWAY_ALIASES = {"hammer": "slurm-hammer", "gautschi": "slurm-gautschi"}
_GATEWAY_LIST = ", ".join(_GATEWAYS)

# Upstream-metrics target labels, derived per request so one client can talk
# to several gateway backends and still be broken down by backend.
_HOST_TO_GATEWAY = {httpx.URL(url).host: gw for gw, url in _GATEWAYS.items()}


def _gateway_target(request: httpx.Request) -> str:
    return f"dask-gateway-{_HOST_TO_GATEWAY.get(request.url.host, 'unknown')}"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=instrumented_transport(_gateway_target))


def _resolve_gateway(name: str) -> tuple[str, str]:
    """Return (canonical_name, url) or raise ValueError."""
    key = _GATEWAY_ALIASES.get(name.lower(), name.lower())
    if key not in _GATEWAYS:
        raise ValueError(f"Unknown gateway '{name}'. Valid options: {_GATEWAY_LIST}")
    return key, _GATEWAYS[key]


def _auth(username: str) -> dict:
    """HTTP Basic for SimpleAuthenticator (password field ignored when unset)."""
    cred = base64.b64encode(f"{username}:".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


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


def register(mcp) -> None:
    @mcp.tool()
    async def list_dask_clusters() -> str:
        """List all running Dask clusters across every gateway backend.

        Queries the Kubernetes gateway and all SLURM gateways concurrently and
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
    async def get_dask_cluster_info(cluster_name: str, gateway: str = "k8s") -> str:
        """Get detailed information about a specific Dask cluster.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
            gateway: Gateway backend — 'k8s' (default), 'slurm-hammer',
                     'slurm-gautschi', or 'slurm'. Use the value shown in
                     list_dask_clusters.
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
    async def scale_dask_cluster(
        cluster_name: str, n_workers: int, gateway: str = "k8s"
    ) -> str:
        """Scale a Dask cluster to the requested number of workers.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
            n_workers: Target worker count (≥ 0).
            gateway: Gateway backend — 'k8s' (default), 'slurm-hammer',
                     'slurm-gautschi', or 'slurm'.
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
            gateway: Gateway backend — 'k8s' (default), 'slurm-hammer',
                     'slurm-gautschi', or 'slurm'.
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
