"""Log querying tools — notebook and Dask pods via Loki."""

import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from context import require_user
from errors import (
    UserError,
    http_error,
    json_body,
    malformed_response,
    response_detail,
    unreachable,
)
from shared import quote_label, shared_client

LOKI_URL = os.environ.get("LOKI_URL", "http://loki.cms.svc.cluster.local:3100")


# ── time helpers ──────────────────────────────────────────────────────────────


def _to_ns(value: str, now_s: float) -> str:
    """Convert a human time spec to a Loki nanosecond Unix timestamp string.

    Accepts:
      'now'                    → current time
      '2d', '1h', '30m', '90s' → that duration ago from now_s
      ISO-8601 with timezone   → parsed as-is
      ISO-8601 without timezone → assumed UTC (Loki runs in UTC)
    """
    value = value.strip()
    if value.lower() == "now":
        return str(int(now_s * 1e9))

    m = re.fullmatch(r"(\d+)([dhms])", value)
    if m:
        unit = {"d": 86400, "h": 3600, "m": 60, "s": 1}[m.group(2)]
        return str(int((now_s - int(m.group(1)) * unit) * 1e9))

    try:
        iso = value.replace("Z", "+00:00")
        # fromisoformat handles strings with "+HH:MM" offset notation
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # assume UTC when absent
        return str(int(dt.timestamp() * 1e9))
    except ValueError:
        return value  # pass through raw; Loki will surface any format error


# ── deduplication ─────────────────────────────────────────────────────────────


def _msg_key(line: str) -> str:
    """Strip the timestamp from a log line for dedup comparison.

    Keeps source (pod/container) so messages from different pods are
    never collapsed together.
    """
    idx = line.find("] ")
    return line[idx + 2 :] if idx >= 0 else line


def _dedup(lines: list[str]) -> list[str]:
    """Collapse consecutive identical log messages.

    Keeps the first occurrence; appends '  ↑ × N identical' for runs of N > 1.
    """
    if not lines:
        return lines

    result: list[str] = []
    run = 1
    for i in range(len(lines)):
        if i == 0:
            result.append(lines[i])
            continue
        if _msg_key(lines[i]) == _msg_key(lines[i - 1]):
            run += 1
        else:
            if run > 1:
                result.append(f"  ↑ × {run} identical lines omitted")
            result.append(lines[i])
            run = 1
    if run > 1:
        result.append(f"  ↑ × {run} identical lines omitted")
    return result


# ── Loki query ────────────────────────────────────────────────────────────────


async def _loki_query(
    selector: str,
    start: str,
    end: Optional[str],
    limit: int,
    direction: str,
    dedup: bool,
) -> str:
    now_s = time.time()
    start_param = _to_ns(start, now_s)
    end_param = _to_ns(end, now_s) if end else None

    capped = min(limit, 5000)
    params: dict = {
        "query": selector,
        "start": start_param,
        "limit": capped,
        "direction": direction,
    }
    if end_param:
        params["end"] = end_param

    client = shared_client("loki")
    try:
        resp = await client.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params=params,
            timeout=30.0,
        )
    except httpx.RequestError as exc:
        raise unreachable("the log store (Loki)", exc)

    if resp.status_code == 400:
        # Loki's 400 is a query it could not parse — the caller's filter or
        # time range, not the facility.
        raise UserError(
            "Error: the log store (Loki) rejected the query (HTTP 400) — "
            f"{response_detail(resp, limit=400) or 'no reason given'}. The filter "
            "argument must be a LogQL pipe expression such as '|= \"ERROR\"' or "
            "'|~ \"timeout|refused\"', and start/end must be durations ('1h', "
            "'2d') or ISO-8601 timestamps."
        )
    if resp.status_code != 200:
        raise http_error("the log store (Loki)", resp, action="query logs")

    payload = json_body(resp)
    data = payload.get("data") if isinstance(payload, dict) else None
    streams = data.get("result") if isinstance(data, dict) else None
    if not isinstance(streams, list):
        raise malformed_response("the log store (Loki)", resp, "a log query result")
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

    # Judge truncation on the raw count — dedup can shrink the output below
    # the cap while Loki still cut the window short.
    truncated = len(lines) >= capped

    if dedup:
        lines = _dedup(lines)

    header = f"# {len(lines)} line(s)"
    if truncated:
        header += f" (limit={limit} reached — narrow the time range or add a filter)"
    return header + "\n\n" + "\n".join(lines)


# ── tools ─────────────────────────────────────────────────────────────────────


def register(mcp: Any) -> None:
    @mcp.tool()
    async def query_notebook_logs(
        start: str = "1h",
        end: Optional[str] = None,
        limit: int = 500,
        direction: str = "forward",
        filter: Optional[str] = None,
        dedup: bool = True,
    ) -> str:
        """Query Loki for logs from the notebook container of the authenticated user's pod.

        Covers the JupyterLab / VS Code server process — not Dask workers.
        Use query_dask_logs for distributed computation logs.

        Args:
            start: How far back to look — duration ('1h', '30m', '2d') or
                   ISO-8601 timestamp (timezone-aware; bare timestamps assumed
                   UTC). Default: '1h'.
            end:   End boundary — duration ago ('2h' = 2 hours ago), 'now', or
                   ISO-8601 timestamp. Default: now.
            limit: Maximum log lines to return. Default: 500.
            direction: 'forward' (oldest first, default) or 'backward' (newest first).
            filter: Optional LogQL pipe expression, e.g. '|= "ERROR"' or
                    '|~ "timeout|refused"'.
            dedup: Collapse consecutive identical messages (default True).
                   Useful when errors are flooding the log.
        """
        user = require_user()
        namespace = user["namespace"]
        username = user["username"]
        # Scope by username + notebook container — no Hub admin pod_name needed.
        # quote_label keeps a crafted name from breaking out of the matcher.
        selector = (
            f'{{namespace="{quote_label(namespace)}",'
            f'username="{quote_label(username)}",container="notebook"}}'
        )
        if filter:
            selector = f"{selector} {filter}"
        return await _loki_query(selector, start, end, limit, direction, dedup)

    @mcp.tool()
    async def query_dask_logs(
        start: str = "1h",
        end: Optional[str] = None,
        limit: int = 500,
        direction: str = "forward",
        filter: Optional[str] = None,
        dedup: bool = True,
    ) -> str:
        """Query Loki for logs from the authenticated user's Dask worker and scheduler pods.

        Excludes the JupyterHub notebook container. Use query_notebook_logs for the
        interactive server logs.

        Args:
            start: How far back to look — duration ('1h', '30m', '2d') or
                   ISO-8601 timestamp. Default: '1h'.
            end:   End boundary — duration ago, 'now', or ISO-8601. Default: now.
            limit: Maximum log lines to return. Default: 500.
            direction: 'forward' (oldest first, default) or 'backward' (newest first).
            filter: Optional LogQL pipe expression.
            dedup: Collapse consecutive identical messages (default True).
        """
        user = require_user()
        namespace = user["namespace"]
        username = user["username"]
        selector = (
            f'{{namespace="{quote_label(namespace)}",'
            f'username="{quote_label(username)}",container!="notebook"}}'
        )
        if filter:
            selector = f"{selector} {filter}"
        return await _loki_query(selector, start, end, limit, direction, dedup)
