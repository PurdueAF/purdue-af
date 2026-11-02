"""Cross-tool helpers: pooled HTTP clients, query-label escaping, and asking
Prometheus so that "Prometheus is down" and "there is no such series" can never
be confused (see errors.py)."""

from typing import Any, Optional

import httpx
from errors import describe_exception, json_body, response_detail
from metrics import instrumented_transport

_clients: dict[str, httpx.AsyncClient] = {}


class _CookielessTransport(httpx.AsyncBaseTransport):
    """Transport wrapper that removes ``Set-Cookie`` from every response."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        # Dask Gateway prefers its session cookie over the Authorization header.
        if "set-cookie" in response.headers:
            del response.headers["set-cookie"]
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def upstream_transport(name: str, **transport_kwargs: Any) -> httpx.AsyncBaseTransport:
    """The transport for a client shared between users: metered, and it drops
    the cookies a backend sets, so the caller's token is a request's only identity."""
    return _CookielessTransport(instrumented_transport(name, **transport_kwargs))


def shared_client(name: str, **transport_kwargs: Any) -> httpx.AsyncClient:
    """Return the process-wide pooled client for ``name``, creating it once.

    Never close one at a call site. ``name`` is also the upstream-metrics target
    label. ``transport_kwargs`` (e.g. ``verify=``) apply only on first creation.
    """
    client = _clients.get(name)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            transport=upstream_transport(name, **transport_kwargs)
        )
        _clients[name] = client
    return client


def quote_label(value: str) -> str:
    """Escape a value for a double-quoted PromQL/LogQL label matcher."""
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
    caller completes with the backend's name in the user's terms.
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


async def prom_value(
    client: httpx.AsyncClient,
    base_url: str,
    query: str,
    *,
    timeout: float = 8.0,
) -> tuple[Optional[float], Optional[str]]:
    """``prom_query`` for one number → ``(first value or None, problem)``."""
    rows, problem = await prom_query(client, base_url, query, timeout=timeout)
    return prom_scalar(rows), problem


async def prom_samples(
    client: httpx.AsyncClient,
    base_url: str,
    query: str,
    *,
    timeout: float = 8.0,
) -> tuple[list[tuple[dict[str, Any], float]], Optional[str]]:
    """``prom_query`` for a vector → ``([(labels, value), …], problem)``."""
    rows, problem = await prom_query(client, base_url, query, timeout=timeout)
    return prom_vector(rows), problem


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
