"""Any PromQL query against the facility's two Prometheus instances, with what
it sees limited by promql_scope: series in cms, and of the series that belong
to one person, the caller's own."""

import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from annotations import READS
from config import (
    CLUSTER_PROMETHEUS_NAME,
    CLUSTER_PROMETHEUS_URL,
    PROMETHEUS_NAME,
    PROMETHEUS_URL,
)
from context import require_user
from errors import UpstreamError, UserError, describe_exception, json_body
from mcp.server.mcpserver import MCPServer
from promql_scope import resolve, scope
from shared import shared_client

# name -> (shared client, URL, how messages name it)
SOURCES = {
    "af": ("prometheus", PROMETHEUS_URL, PROMETHEUS_NAME),
    "cluster": ("cluster-prometheus", CLUSTER_PROMETHEUS_URL, CLUSTER_PROMETHEUS_NAME),
}
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
DURATION = re.compile(r"(\d+)([smhdw])")
MAX_RANGE = 30 * 86400
MAX_POINTS = 600
DEFAULT_POINTS = 60
MIN_STEP = 15
# The caller's objects are looked up over the query's window and a day before it.
OWNED_LOOKBACK = 86400
QUERY_TIMEOUT = 30
MAX_SERIES = 50
MAX_METRICS = 100
MAX_CHARS = 30_000
SCOPE_NOTE = (
    "Series of other people's sessions, Dask clusters, Ray jobs and home "
    "directories are left out; list_af_profiles and get_ray_capacity have the "
    "free GPUs."
)


def seconds(text: str, what: str) -> int:
    m = DURATION.fullmatch(text.strip())
    if not m:
        raise UserError(f"Error: {what} {text!r} is not a duration like 30m, 6h or 7d.")
    return int(m.group(1)) * UNITS[m.group(2)]


def when(text: str, now: float, what: str) -> float:
    """'now', a duration ago ('6h'), or an ISO 8601 time (UTC when it names no zone)."""
    text = text.strip()
    if text.lower() == "now":
        return now
    if DURATION.fullmatch(text):
        return now - seconds(text, what)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise UserError(
            f"Error: {what} {text!r} is neither 'now', a duration like 6h, "
            "nor an ISO 8601 time."
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def source_of(name: str) -> tuple[str, str, str]:
    found = SOURCES.get(name.strip().lower())
    if found is None:
        raise UserError(f"Error: source {name!r} is neither 'af' nor 'cluster'.")
    return found


def labels_text(metric: dict[str, Any]) -> str:
    name = metric.get("__name__", "")
    rest = ", ".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    return f"{name}{{{rest}}}" if rest or not name else name


def number(value: Any) -> str:
    try:
        return f"{float(value):.6g}"
    except (TypeError, ValueError):
        return str(value)


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clip(lines: list[str]) -> str:
    text = "\n".join(lines)
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS].rsplit("\n", 1)[0] + "\n… (cut; narrow the query)"


def render(data: dict[str, Any]) -> list[str]:
    kind, result = data.get("resultType"), data.get("result")
    if kind in ("scalar", "string") and isinstance(result, list) and len(result) == 2:
        return [f"{kind}: {number(result[1])}"]
    if not isinstance(result, list):
        return ["(no result)"]
    if not result:
        return ["No series match."]
    lines = [
        f"{len(result)} series"
        + (f", first {MAX_SERIES}:" if len(result) > MAX_SERIES else ":")
    ]
    if kind == "vector":
        rows = sorted(result, key=lambda r: labels_text(r.get("metric") or {}))
        for row in rows[:MAX_SERIES]:
            value = (row.get("value") or [None, None])[1]
            lines.append(f"- {labels_text(row.get('metric') or {})}: {number(value)}")
    else:
        for row in result[:MAX_SERIES]:
            points = row.get("values") or []
            values = ", ".join(number(v) for _, v in points)
            first = iso(float(points[0][0])) if points else "-"
            lines.append(
                f"- {labels_text(row.get('metric') or {})} from {first}: {values}"
            )
    return lines


async def ask(
    client: httpx.AsyncClient, url: str, name: str, path: str, form: dict[str, str]
) -> dict[str, Any]:
    """Prometheus' data for a query, or the Failure that says why there is none."""
    try:
        # POST: a scoped query is longer than the Rancher proxy takes in a URL.
        resp = await client.post(f"{url}{path}", data=form, timeout=QUERY_TIMEOUT + 5)
    except httpx.RequestError as exc:
        raise UpstreamError(
            f"Error: {name} is unreachable — {describe_exception(exc)}."
        )
    body = json_body(resp)
    if not isinstance(body, dict):
        raise UpstreamError(f"Error: {name} returned HTTP {resp.status_code}.")
    if resp.status_code in (400, 422):
        raise UserError(f"Error: {name} refused the query — {body.get('error')}.")
    if resp.status_code == 503 or body.get("errorType") == "timeout":
        raise UpstreamError(
            f"Error: {name} could not answer in time — {body.get('error')}. "
            "Narrow the query: fewer series, a shorter range or a larger step."
        )
    data = body.get("data")
    if resp.status_code != 200 or not isinstance(data, dict):
        raise UpstreamError(f"Error: {name} returned HTTP {resp.status_code}.")
    return data


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READS)
    async def query_prometheus(
        query: str,
        source: str = "af",
        start: Optional[str] = None,
        end: Optional[str] = None,
        step: Optional[str] = None,
    ) -> str:
        """Run a PromQL query against the facility's monitoring.

        Every series in the cms namespace is visible, except the ones that
        belong to someone else: series of another person's session, Dask
        cluster, Ray job or home directory are left out before the query
        runs, so a total over everyone's sessions or workers counts only
        yours. Free GPUs come from list_af_profiles and get_ray_capacity, not
        from here. list_prometheus_metrics lists the metric names. The shared
        volumes are kubelet_volume_stats_* on 'cluster', by
        persistentvolumeclaim: /work is af-shared-storage.

        Args:
            query: PromQL.
            source: 'af' (sessions, storage, Dask schedulers, node and
                facility health, alerts) or 'cluster' (container_* CPU and
                memory, kube-state-metrics, Dask workers, service metrics).
            start: For a range query, how far back it starts — a duration
                ('6h', '7d'; at most 30d) or an ISO 8601 time. Omit it for
                one instant.
            end: The range's end, or the instant: 'now', a duration ago, or
                an ISO 8601 time. Default: now.
            step: The range query's resolution, like '5m'. Default: the
                range over 60 points.
        """
        client_name, url, name = source_of(source)
        now = time.time()
        until = when(end, now, "end") if end else now
        span = round(until - when(start, now, "start")) if start else 0
        if start and span <= 0:
            raise UserError("Error: start must come before end.")
        if span > MAX_RANGE:
            raise UserError("Error: a range query covers at most 30d.")
        client = shared_client(client_name)
        user = require_user()
        owned = await resolve(
            shared_client("prometheus"),
            user["username"],
            until - span - OWNED_LOOKBACK,
            until,
        )
        scoped = await scope(query, owned)
        if span:
            every = (
                seconds(step, "step") if step else max(MIN_STEP, span // DEFAULT_POINTS)
            )
            if every < 1:
                raise UserError("Error: step must be at least 1s.")
            if span // every > MAX_POINTS:
                raise UserError(
                    f"Error: this range needs a step of at least "
                    f"{span // MAX_POINTS}s ({MAX_POINTS} points)."
                )
            data = await ask(
                client,
                url,
                name,
                "/api/v1/query_range",
                {
                    "query": scoped,
                    "start": f"{until - span:.3f}",
                    "end": f"{until:.3f}",
                    "step": str(every),
                    "timeout": f"{QUERY_TIMEOUT}s",
                },
            )
            head = f"**{source}**, {iso(until - span)} to {iso(until)}, every {every}s"
        else:
            data = await ask(
                client,
                url,
                name,
                "/api/v1/query",
                {
                    "query": scoped,
                    "time": f"{until:.3f}",
                    "timeout": f"{QUERY_TIMEOUT}s",
                },
            )
            head = f"**{source}**, at {iso(until)}"
        return clip([head, *render(data), "", SCOPE_NOTE])

    @mcp.tool(annotations=READS)
    async def list_prometheus_metrics(search: str = "", source: str = "af") -> str:
        """Metric names query_prometheus can read, with their help text.

        Args:
            search: Words that the name or the help text contains, like
                'memory' or 'dask_scheduler'. Omit it to list the first names.
            source: 'af' or 'cluster', as for query_prometheus.
        """
        client_name, url, name = source_of(source)
        client = shared_client(client_name)
        user = require_user()
        now = time.time()
        owned = await resolve(
            shared_client("prometheus"), user["username"], now - 3600, now
        )
        names_query = await scope('group by (__name__) ({__name__=~".+"})', owned)
        names_data = await ask(
            client,
            url,
            name,
            "/api/v1/query",
            {"query": names_query, "timeout": f"{QUERY_TIMEOUT}s"},
        )
        names = [
            str((row.get("metric") or {}).get("__name__"))
            for row in names_data.get("result") or []
        ]
        try:
            meta_resp = await client.get(
                f"{url}/api/v1/metadata",
                params={"limit_per_metric": "1"},
                timeout=QUERY_TIMEOUT,
            )
        except httpx.RequestError as exc:
            raise UpstreamError(
                f"Error: {name} is unreachable — {describe_exception(exc)}."
            )
        meta_body = json_body(meta_resp)
        meta = meta_body.get("data") if isinstance(meta_body, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        words = search.lower().split()
        lines = []
        for metric in sorted(names):
            about = (meta.get(metric) or [{}])[0]
            text = (
                f"{metric} ({about.get('type', '?')}): {about.get('help', '')}".strip()
            )
            if all(w in text.lower() for w in words):
                lines.append(f"- {text}")
        if not lines:
            return f"No metric on {source} matches {search!r}."
        shown = lines[:MAX_METRICS]
        more = len(lines) - len(shown)
        tail = [f"… and {more} more; search for narrower words."] if more else []
        return clip([f"{len(lines)} metric(s) on {source}:", *shown, *tail])
