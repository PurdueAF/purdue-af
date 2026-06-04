"""Storage quota tool — reads af-pod-monitor metrics from localhost:9090."""

from datetime import datetime, timezone
from typing import Optional

import httpx

_MONITOR_URL = "http://localhost:9090/metrics"
_DIRS = ("home", "work")


def _parse_gauge(lines: list[str], name: str) -> Optional[float]:
    """Extract a scalar gauge value from Prometheus text-format lines."""
    for line in lines:
        if line.startswith("#"):
            continue
        if line.startswith(name + " ") or line.startswith(name + "{"):
            try:
                return float(line.split()[-1])
            except (ValueError, IndexError):
                pass
    return None


def _bar(fraction: float, width: int = 20) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "░" * (width - filled)


def register(mcp) -> None:
    @mcp.tool()
    async def query_storage_usage() -> str:
        """Report storage quota and usage for this user's home and work directories.

        Data is sourced from the af-pod-monitor sidecar (refreshed every 5 minutes).
        Returns used / total space, utilisation percentage, and last-accessed time
        for each directory.
        """
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(_MONITOR_URL, timeout=5.0)
            except httpx.RequestError as exc:
                return f"Error: could not reach af-pod-monitor at {_MONITOR_URL} — {exc}"

        if resp.status_code != 200:
            return f"Error: af-pod-monitor returned HTTP {resp.status_code}"

        lines = resp.text.splitlines()

        rows: list[str] = ["# Storage usage (data age ≤ 5 min)\n"]
        any_data = False

        for prefix in _DIRS:
            used_kb = _parse_gauge(lines, f"af_{prefix}_dir_used_kb")
            size_kb = _parse_gauge(lines, f"af_{prefix}_dir_size_kb")
            util = _parse_gauge(lines, f"af_{prefix}_dir_util")
            last_accessed = _parse_gauge(lines, f"af_{prefix}_dir_last_accessed")

            if used_kb is None or size_kb is None:
                rows.append(f"/{prefix}/: no data\n")
                continue

            any_data = True
            used_gb = used_kb / 1024 / 1024
            size_gb = size_kb / 1024 / 1024
            pct = (util or (used_kb / size_kb if size_kb else 0)) * 100

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
                "No storage metrics available — af-pod-monitor may still be initialising "
                "(first reading takes up to 5 minutes after pod start)."
            )

        return "\n".join(rows)
