import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import uvicorn
from mcp import types
from mcp.server import Server, ServerRequestContext

logger = logging.getLogger(__name__)

NB_USER = os.environ.get("NB_USER", "")
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

        await self._app(scope, receive, send)

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


async def handle_list_tools(
    ctx: ServerRequestContext,
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="query_loki_logs",
                description=(
                    "Query Loki for logs from all pods belonging to this Analysis Facility "
                    "user (notebook pod + Dask workers). Returns timestamped log lines."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "start": {
                            "type": "string",
                            "description": (
                                "How far back to look. Duration ('1h', '30m', '2h') "
                                "or ISO-8601 timestamp. Default: '1h'."
                            ),
                        },
                        "end": {
                            "type": "string",
                            "description": "ISO-8601 end timestamp. Default: now.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum log lines to return. Default: 500.",
                        },
                        "filter": {
                            "type": "string",
                            "description": (
                                "Optional LogQL pipe expression appended to the selector. "
                                "Examples: '|= \"ERROR\"' or '|~ \"timeout|refused\"'."
                            ),
                        },
                    },
                },
            )
        ]
    )


async def handle_call_tool(
    ctx: ServerRequestContext,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    if params.name != "query_loki_logs":
        return types.CallToolResult(
            isError=True,
            content=[
                types.TextContent(type="text", text=f"Unknown tool: {params.name}")
            ],
        )

    args = params.arguments or {}
    start_raw: str = args.get("start", "1h")
    end_raw: Optional[str] = args.get("end")
    limit: int = min(int(args.get("limit", 500)), 5000)
    filter_expr: Optional[str] = args.get("filter")

    selector = f'{{namespace="{NAMESPACE}",username="{NB_USER}"}}'
    if filter_expr:
        selector = f"{selector} {filter_expr}"

    m = re.fullmatch(r"(\d+)([hms])", start_raw)
    if m:
        secs = int(m.group(1)) * {"h": 3600, "m": 60, "s": 1}[m.group(2)]
        start_param = str(int((time.time() - secs) * 1e9))
    else:
        start_param = start_raw

    loki_params: dict = {
        "query": selector,
        "start": start_param,
        "limit": limit,
        "direction": "forward",
    }
    if end_raw:
        loki_params["end"] = end_raw

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params=loki_params,
                timeout=30.0,
            )
        except httpx.RequestError as exc:
            return types.CallToolResult(
                isError=True,
                content=[
                    types.TextContent(type="text", text=f"Loki connection error: {exc}")
                ],
            )

    if resp.status_code != 200:
        return types.CallToolResult(
            isError=True,
            content=[
                types.TextContent(
                    type="text", text=f"Loki HTTP {resp.status_code}: {resp.text[:500]}"
                )
            ],
        )

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
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text", text="No logs found for the specified time range."
                )
            ]
        )

    header = f"# {len(lines)} log line(s)"
    if len(lines) == limit:
        header += f" (limit={limit} reached — narrow the time range or add a filter)"
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text=header + "\n\n" + "\n".join(lines))
        ]
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    if not NB_USER:
        logger.warning("NB_USER is not set — token ownership check will always fail")

    server = Server(
        "purdue-af-sidecar",
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )

    starlette_app = server.streamable_http_app(stateless_http=True, json_response=True)
    starlette_app = _AuthMiddleware(starlette_app)

    uvicorn.run(starlette_app, host="0.0.0.0", port=9191, log_level="info")


if __name__ == "__main__":
    main()
