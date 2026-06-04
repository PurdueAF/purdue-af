"""Log querying tools — notebook and Dask pods via Loki."""

import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from context import current_user

LOKI_URL = os.environ.get("LOKI_URL", "http://loki.cms.svc.cluster.local:3100")


async def _loki_query(selector: str, start: str, end: Optional[str], limit: int) -> str:
    m = re.fullmatch(r"(\d+)([hms])", start)
    if m:
        secs = int(m.group(1)) * {"h": 3600, "m": 60, "s": 1}[m.group(2)]
        start_param = str(int((time.time() - secs) * 1e9))
    else:
        start_param = start

    capped = min(limit, 5000)
    params: dict = {
        "query": selector,
        "start": start_param,
        "limit": capped,
        "direction": "forward",
    }
    if end:
        params["end"] = end

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params=params,
                timeout=30.0,
            )
        except httpx.RequestError as exc:
            return f"Error: Loki connection failed — {exc}"

    if resp.status_code != 200:
        return f"Error: Loki returned HTTP {resp.status_code} — {resp.text[:500]}"

    streams = resp.json().get("data", {}).get("result", [])
    lines: list[str] = []
    for stream in streams:
        labels = stream.get("stream", {})
        pod = labels.get("pod", "unknown")
        container = labels.get("container", "unknown")
        for ts_ns, log_line in stream.get("values", []):
            ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
            lines.append(
                f"[{ts.strftime('%Y-%m-%dT%H:%M:%SZ')}] {pod}/{container}: {log_line}"
            )

    if not lines:
        return "No logs found for the specified time range."

    header = f"# {len(lines)} log line(s)"
    if len(lines) == capped:
        header += f" (limit={limit} reached — narrow the time range or add a filter)"
    return header + "\n\n" + "\n".join(lines)


def register(mcp) -> None:
    @mcp.tool()
    async def query_notebook_logs(
        start: str = "1h",
        end: Optional[str] = None,
        limit: int = 500,
        filter: Optional[str] = None,
    ) -> str:
        """Query Loki for logs from the notebook container of the authenticated user's pod.

        Covers the JupyterLab / VS Code server process — not Dask workers.
        Use query_dask_logs for distributed computation logs.

        Args:
            start: How far back to look — duration ('1h', '30m', '2h') or ISO-8601
                   timestamp. Default: '1h'.
            end: ISO-8601 end timestamp. Default: now.
            limit: Maximum log lines to return. Default: 500.
            filter: Optional LogQL pipe expression, e.g. '|= "ERROR"' or
                    '|~ "timeout|refused"'.
        """
        user = current_user.get()
        namespace = user["namespace"]
        username = user["username"]
        pod_name = user["pod_name"]

        if not pod_name:
            return "Error: no running server found for this user — start a pod first."

        selector = (
            f'{{namespace="{namespace}",username="{username}",'
            f'pod="{pod_name}",container="notebook"}}'
        )
        if filter:
            selector = f"{selector} {filter}"
        return await _loki_query(selector, start, end, limit)

    @mcp.tool()
    async def query_dask_logs(
        start: str = "1h",
        end: Optional[str] = None,
        limit: int = 500,
        filter: Optional[str] = None,
    ) -> str:
        """Query Loki for logs from the authenticated user's Dask worker and scheduler pods.

        Excludes the JupyterHub notebook pod. Use query_notebook_logs for the
        interactive server logs.

        Args:
            start: How far back to look — duration ('1h', '30m', '2h') or ISO-8601
                   timestamp. Default: '1h'.
            end: ISO-8601 end timestamp. Default: now.
            limit: Maximum log lines to return. Default: 500.
            filter: Optional LogQL pipe expression, e.g. '|= "ERROR"' or
                    '|~ "timeout|refused"'.
        """
        user = current_user.get()
        namespace = user["namespace"]
        username = user["username"]
        pod_name = user["pod_name"]

        selector = (
            f'{{namespace="{namespace}",username="{username}"'
            + (f',pod!="{pod_name}"' if pod_name else "")
            + "}"
        )
        if filter:
            selector = f"{selector} {filter}"
        return await _loki_query(selector, start, end, limit)
