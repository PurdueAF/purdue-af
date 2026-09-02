"""Storage quota tool — queries af-pod-monitor metrics from Prometheus."""

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from context import require_user
from shared import prom_query, prom_scalar, quote_label, shared_client

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-server:9090")
_DIRS = ("home", "work")


async def _prom_scalar(
    client: httpx.AsyncClient, query: str
) -> tuple[Optional[float], Optional[str]]:
    """Run an instant PromQL query → (first scalar or None, problem or None).

    The problem is set only when Prometheus could not be asked; a None value
    with no problem means the series does not exist.
    """
    rows, problem = await prom_query(client, PROMETHEUS_URL, query, timeout=5.0)
    return prom_scalar(rows), problem


def _bar(fraction: float, width: int = 20) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "░" * (width - filled)


def register(mcp: Any) -> None:
    @mcp.tool()
    async def query_storage_usage() -> str:
        """Report storage quota and usage for the authenticated user's home and work directories.

        Data is sourced from Prometheus (scraped from af-pod-monitor, refreshed every
        5 minutes). Returns used / total space, utilisation percentage, and
        last-accessed time for each directory.
        """
        user = require_user()
        username = user["username"]
        # Metrics are labeled by username via Kubernetes SD (username_unescaped).
        # No Hub admin state / pod_name required.
        user_selector = f'username="{quote_label(username)}"'

        client = shared_client("prometheus")

        async def _dir_metrics(
            prefix: str,
        ) -> list[tuple[Optional[float], Optional[str]]]:
            return await asyncio.gather(
                *(
                    _prom_scalar(client, f"af_{prefix}_dir_{metric}{{{user_selector}}}")
                    for metric in ("used_kb", "size_kb", "util", "last_accessed")
                )
            )

        # Query both directories concurrently; rows still render home-then-work.
        per_dir = await asyncio.gather(*(_dir_metrics(prefix) for prefix in _DIRS))

        # "Prometheus is down" must never read as "no data": collect every
        # problem and report the first if nothing at all could be read.
        problems = [p for results in per_dir for _, p in results if p]

        rows: list[str] = ["# Storage usage (data age ≤ 5 min)\n"]
        any_data = False

        for prefix, results in zip(_DIRS, per_dir):
            used_kb, size_kb, util, last_accessed = (v for v, _ in results)
            if used_kb is None or size_kb is None:
                rows.append(f"/{prefix}/: no data\n")
                continue

            any_data = True
            used_gb = used_kb / 1024 / 1024
            size_gb = size_kb / 1024 / 1024
            pct = (
                util if util is not None else (used_kb / size_kb if size_kb else 0)
            ) * 100

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
            if problems:
                return (
                    f"Error: could not read storage metrics — Prometheus {problems[0]}. This "
                    "is a monitoring problem, not a quota problem: your storage is "
                    "unaffected. Try again in a minute; get_facility_health shows "
                    "whether the facility itself is degraded."
                )
            return (
                "No storage metrics in Prometheus for this user — the session may "
                "not be running, or af-pod-monitor may still be initialising "
                "(first reading takes up to 5 minutes after pod start)."
            )

        if problems:
            rows.append(
                f"Note: some readings could not be fetched — Prometheus {problems[0]}. "
                "Missing figures above are a monitoring gap, not empty storage."
            )

        return "\n".join(rows)
