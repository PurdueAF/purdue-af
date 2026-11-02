"""Storage quota tool — queries af-pod-monitor metrics from Prometheus."""

import asyncio
from typing import Optional

from config import PROMETHEUS_NAME, PROMETHEUS_URL
from context import require_user
from errors import UpstreamError
from mcp.server.mcpserver import MCPServer
from shared import prom_value, quote_label, shared_client

_DIRS = ("home", "work")


def dir_reading_query(prefix: str, metric: str, selector: str) -> str:
    """A directory's metric, empty unless the last pass read it: a failed or
    skipped read keeps the gauge's old value and sets only af_*_dir_ok to 0."""
    return (
        f"af_{prefix}_dir_{metric}{{{selector}}}"
        f" and on(pod) af_{prefix}_dir_ok{{{selector}}} == 1"
    )


def _bar(fraction: float, width: int = 20) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "░" * (width - filled)


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def query_storage_usage() -> str:
        """Report storage quota and usage for the authenticated user's home and work directories.

        Data comes from af-pod-monitor via Prometheus, refreshed every few
        minutes. Returns used / total space and utilisation percentage for
        each directory.
        """
        user = require_user()
        username = user["username"]
        # Metrics are labeled by username via Kubernetes SD (username_unescaped).
        user_selector = f'username="{quote_label(username)}"'

        client = shared_client("prometheus")

        async def _dir_metrics(
            prefix: str,
        ) -> list[tuple[Optional[float], Optional[str]]]:
            return await asyncio.gather(
                *(
                    prom_value(
                        client,
                        PROMETHEUS_URL,
                        dir_reading_query(prefix, metric, user_selector),
                        timeout=5.0,
                    )
                    for metric in ("used_kb", "size_kb", "util")
                )
            )

        # Query both directories concurrently; rows still render home-then-work.
        per_dir = await asyncio.gather(*(_dir_metrics(prefix) for prefix in _DIRS))

        # "Prometheus is down" must never read as "no data".
        problems = [p for results in per_dir for _, p in results if p]

        rows: list[str] = ["# Storage usage (data age ≤ 5 min)\n"]
        any_data = False

        for prefix, results in zip(_DIRS, per_dir):
            used_kb, size_kb, util = (v for v, _ in results)
            if used_kb is None or size_kb is None:
                rows.append(f"/{prefix}/: no data\n")
                continue

            any_data = True
            used_gb = used_kb / 1024 / 1024
            size_gb = size_kb / 1024 / 1024
            pct = (
                util if util is not None else (used_kb / size_kb if size_kb else 0)
            ) * 100

            rows.append(
                f"/{prefix}/\n"
                f"  {used_gb:.2f} GB / {size_gb:.2f} GB  "
                f"[{_bar(pct / 100)}]  {pct:.1f}%\n"
            )

        if not any_data:
            if problems:
                raise UpstreamError(
                    f"Error: could not read storage metrics — {PROMETHEUS_NAME} "
                    f"{problems[0]}. This "
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
                f"Note: some readings could not be fetched — {PROMETHEUS_NAME} "
                f"{problems[0]}. "
                "Missing figures above are a monitoring gap, not empty storage."
            )

        return "\n".join(rows)
