"""Dask cluster tools — list, inspect, scale, stop via Dask Gateway REST API.

Multiple gateway backends are supported (k8s, slurm-hammer, slurm-gautschi, slurm).
list_dask_clusters queries all of them; the other tools take a `gateway` argument
so the correct backend is targeted.

Gateways here use SimpleAuthenticator (password ignored). Calls authenticate as
the Hub username via HTTP Basic so each user only sees their own clusters.

Worker counts come from the AF Prometheus (dask_scheduler_workers). Live CPU /
memory usage comes from the cluster Prometheus (cadvisor), filtered to Running
worker pods.
"""

import asyncio
import base64
import os
from typing import Optional

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
    key = _GATEWAY_ALIASES.get(name.lower(), name.lower())
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
    async def get_dask_worker_count(cluster_name: str, gateway: str = "k8s") -> str:
        """Return the current number of workers for a Dask cluster (by state).

        Uses the scheduler's Prometheus metrics. Prefer this over guessing from
        list_dask_clusters when you need an accurate live count.

        Args:
            cluster_name: Cluster identifier returned by list_dask_clusters.
            gateway: Gateway backend — 'k8s' (default), 'slurm-hammer',
                     'slurm-gautschi', or 'slurm'.
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
            gateway: Gateway backend — 'k8s' (default), 'slurm-hammer',
                     'slurm-gautschi', or 'slurm'.
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
