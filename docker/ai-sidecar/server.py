import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

NB_USER = os.environ.get("NB_USER", "")
POD_NAME = os.environ.get("POD_NAME", "")
HUB_API_URL = os.environ.get("JUPYTERHUB_API_URL", "http://hub:8081/hub/api")
LOKI_URL = os.environ.get("LOKI_URL", "http://loki.cms.svc.cluster.local:3100")
NAMESPACE = os.environ.get("NAMESPACE", "cms")

# token → (expiry_monotonic, username)
_token_cache: dict[str, tuple[float, str]] = {}
_TOKEN_TTL = 60.0


async def _validate_token(token: str) -> Optional[str]:
    now = time.monotonic()
    cached = _token_cache.get(token)
    if cached and now < cached[0]:
        return cached[1]

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{HUB_API_URL}/user",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
        except httpx.RequestError:
            return None

    if resp.status_code != 200:
        return None

    username = resp.json().get("name")
    if username:
        _token_cache[token] = (now + _TOKEN_TTL, username)
    return username


class _AuthMiddleware:
    """Pure-ASGI middleware that validates JupyterHub Bearer tokens."""

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        # Only serve the /mcp endpoint — return 404 for everything else (e.g.
        # Prometheus scraping /metrics) without running the auth logic.
        path = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self._silent_404(send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode()

        if not auth.startswith("Bearer "):
            await self._http_error(send, 401, "Missing Bearer token")
            return

        token = auth[len("Bearer ") :]
        username = await _validate_token(token)

        if username is None:
            await self._http_error(send, 401, "Invalid JupyterHub token")
            return

        if username != NB_USER:
            await self._http_error(
                send, 403, f"Token is for '{username}', not this pod's user '{NB_USER}'"
            )
            return

        # Rewrite Host to "localhost:9191" so the MCP SDK's built-in DNS-rebinding
        # protection accepts the request.  The SDK allows "localhost:*" by default;
        # bare "localhost" (no port) does not match that wildcard pattern.
        # Our own JupyterHub token check above is the real auth gate here.
        new_headers = [
            (b"host", b"localhost:9191") if k.lower() == b"host" else (k, v)
            for k, v in scope.get("headers", [])
        ]
        scope = {**scope, "headers": new_headers}

        await self._app(scope, receive, send)

    @staticmethod
    async def _silent_404(send) -> None:
        body = b'{"error":"not found"}'
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _http_error(send, status: int, detail: str) -> None:
        body = f'{{"error":"{detail}"}}'.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


mcp = FastMCP(
    "purdue-af-sidecar",
    stateless_http=True,
    instructions=(
        "Tools for querying logs from this Purdue Analysis Facility user pod. "
        "Use query_notebook_logs for JupyterLab/VS Code server logs, "
        "and query_dask_logs for Dask worker and scheduler logs."
    ),
)


# ── shared helper ─────────────────────────────────────────────────────────────

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
            lines.append(f"[{ts.strftime('%Y-%m-%dT%H:%M:%SZ')}] {pod}/{container}: {log_line}")

    if not lines:
        return "No logs found for the specified time range."

    header = f"# {len(lines)} log line(s)"
    if len(lines) == capped:
        header += f" (limit={limit} reached — narrow the time range or add a filter)"
    return header + "\n\n" + "\n".join(lines)


# ── tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def query_notebook_logs(
    start: str = "1h",
    end: Optional[str] = None,
    limit: int = 500,
    filter: Optional[str] = None,
) -> str:
    """Query Loki for logs from the notebook container of this user's JupyterHub pod.

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
    selector = (
        f'{{namespace="{NAMESPACE}",username="{NB_USER}",'
        f'pod="{POD_NAME}",container="notebook"}}'
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
    """Query Loki for logs from this user's Dask worker and scheduler pods.

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
    selector = (
        f'{{namespace="{NAMESPACE}",username="{NB_USER}",pod!="{POD_NAME}"}}'
    )
    if filter:
        selector = f"{selector} {filter}"
    return await _loki_query(selector, start, end, limit)


# ── entry point ───────────────────────────────────────────────────────────────

class _McpAccessFilter(logging.Filter):
    """Suppress uvicorn access-log lines that are not MCP requests.

    Prometheus scrapes /metrics every ~15 s; those 404 entries would otherwise
    dominate the container log.  We keep only lines that mention '/mcp' so that
    actual tool calls and auth errors remain visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "/mcp" in record.getMessage()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logging.getLogger("uvicorn.access").addFilter(_McpAccessFilter())

    if not NB_USER:
        logger.warning("NB_USER is not set — token ownership check will always fail")
    if not POD_NAME:
        logger.warning("POD_NAME is not set — notebook/dask log separation will not work")

    app = _AuthMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=9191, log_level="info")


if __name__ == "__main__":
    main()
