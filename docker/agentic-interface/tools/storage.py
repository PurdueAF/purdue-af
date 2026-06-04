"""Storage quota tool — queries af-pod-monitor metrics from Prometheus."""

import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from context import current_user

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-server:9090")
_DIRS = ("home", "work")


async def _prom_scalar(client: httpx.AsyncClient, query: str) -> Optional[float]:
    """Run an instant PromQL query and return the first scalar result."""
    try:
        resp = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=5.0,
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


def _bar(fraction: float, width: int = 20) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "░" * (width - filled)


def register(mcp) -> None:
    @mcp.tool()
    async def query_storage_usage() -> str:
        """Report storage quota and usage for the authenticated user's home and work directories.

        Data is sourced from Prometheus (scraped from af-pod-monitor, refreshed every
        5 minutes). Returns used / total space, utilisation percentage, and
        last-accessed time for each directory.
        """
        user = current_user.get()
        pod_name = user["pod_name"]

        if not pod_name:
            return "Error: no running server found for this user — start a pod first."

        pod_selector = f'pod="{pod_name}"'

        async with httpx.AsyncClient() as client:
            rows: list[str] = ["# Storage usage (data age ≤ 5 min)\n"]
            any_data = False

            for prefix in _DIRS:
                used_kb = await _prom_scalar(
                    client, f"af_{prefix}_dir_used_kb{{{pod_selector}}}"
                )
                size_kb = await _prom_scalar(
                    client, f"af_{prefix}_dir_size_kb{{{pod_selector}}}"
                )
                util = await _prom_scalar(
                    client, f"af_{prefix}_dir_util{{{pod_selector}}}"
                )
                last_accessed = await _prom_scalar(
                    client, f"af_{prefix}_dir_last_accessed{{{pod_selector}}}"
                )

                if used_kb is None or size_kb is None:
                    rows.append(f"/{prefix}/: no data\n")
                    continue

                any_data = True
                used_gb = used_kb / 1024 / 1024
                size_gb = size_kb / 1024 / 1024
                pct = (util if util is not None else (used_kb / size_kb if size_kb else 0)) * 100

                accessed_str = ""
                if last_accessed:
                    dt = datetime.fromtimestamp(last_accessed, tz=timezone.utc)
                    accessed_str = f"  last accessed {dt.strftime('%Y-%m-%d %H:%M UTC')}"

                rows.append(
                    f"/{prefix}/\n"
                    f"  {used_gb:.2f} GB / {size_gb:.2f} GB  "
                    f"[{_bar(pct / 100)}]  {pct:.1f}%"
                    f"{accessed_str}\n"
                )

        if not any_data:
            return (
                "No storage metrics in Prometheus for this pod — af-pod-monitor may still "
                "be initialising (first reading takes up to 5 minutes after pod start)."
            )

        return "\n".join(rows)
