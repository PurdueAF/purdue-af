"""Cross-tool helpers: pooled HTTP clients and query-label escaping.

Every tool used to build a fresh ``httpx.AsyncClient`` per call, paying a TCP
handshake each time; ``shared_client`` keeps one pooled client per backend
target instead (auth.py predates this and keeps its own). Clients live for the
process — never ``async with`` them closed at a call site.

``quote_label`` escapes a value for interpolation inside a PromQL/LogQL label
matcher (``{label="<value>"}``). Usernames come from the Hub and are the only
dynamic values we interpolate, but escaping centrally removes the injection
class outright.

``prom_query`` is the one way tools ask Prometheus, so that "Prometheus is
down" and "there is no such series" can never be confused (see errors.py).
"""

from typing import Any, Callable, Optional, Union

import httpx
from errors import describe_exception, json_body, response_detail
from metrics import instrumented_transport

_clients: dict[str, httpx.AsyncClient] = {}


def shared_client(
    name: str,
    *,
    target: Union[str, Callable[[httpx.Request], str], None] = None,
    **transport_kwargs: Any,
) -> httpx.AsyncClient:
    """Return the process-wide pooled client for ``name``, creating it once.

    ``target`` is the upstream-metrics label (defaults to ``name``); it may be
    a callable when one client talks to several backends (see dask.py).
    ``transport_kwargs`` (e.g. ``verify=``) apply only on first creation.
    """
    client = _clients.get(name)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            transport=instrumented_transport(target or name, **transport_kwargs)
        )
        _clients[name] = client
    return client


def quote_label(value: str) -> str:
    """Escape a string for use inside a double-quoted PromQL/LogQL label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ── Prometheus ────────────────────────────────────────────────────────────────


async def prom_query(
    client: httpx.AsyncClient,
    base_url: str,
    query: str,
    *,
    timeout: float = 8.0,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Instant PromQL query → ``(rows, problem)``.

    ``problem`` is None whenever Prometheus answered the query — an empty
    ``rows`` then genuinely means "no such series", which every caller must
    keep distinct from "could not ask". When set it is a predicate the
    caller completes with the backend's name in the user's terms
    ("Prometheus is unreachable — …", "the monitoring system returned HTTP
    503 — …").
    """
    try:
        resp = await client.get(
            f"{base_url}/api/v1/query", params={"query": query}, timeout=timeout
        )
    except httpx.RequestError as exc:
        return [], f"is unreachable — {describe_exception(exc)}"
    if resp.status_code != 200:
        detail = response_detail(resp, limit=160)
        problem = f"returned HTTP {resp.status_code}"
        return [], f"{problem} — {detail}" if detail else problem
    payload = json_body(resp)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return [], "returned HTTP 200 but the body was not a query result"
    result = data.get("result")
    return (
        [r for r in result if isinstance(r, dict)] if isinstance(result, list) else []
    ), None


def prom_scalar(rows: list[dict[str, Any]]) -> Optional[float]:
    """First sample value of an instant-query result, or None when empty/malformed."""
    if not rows:
        return None
    try:
        return float(rows[0]["value"][1])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def prom_vector(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], float]]:
    """(labels, value) per sample of an instant-query result, skipping malformed rows."""
    out: list[tuple[dict[str, Any], float]] = []
    for row in rows:
        try:
            out.append((row.get("metric") or {}, float(row["value"][1])))
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return out
