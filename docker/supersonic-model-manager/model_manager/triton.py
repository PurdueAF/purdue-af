"""Talking to the Triton servers behind SuperSONIC.

Servers are addressed individually (the SuperSONIC Triton Service is headless,
so there is no single endpoint that reaches every replica). Model state is
therefore per-server, and load/unload fans out to all of them.
"""

import asyncio
import logging

import httpx

from . import kube
from .config import settings

log = logging.getLogger(__name__)

# Message Triton returns when it was not started with --model-control-mode=explicit
_EXPLICIT_HINTS = (
    "polling is enabled",
    "model control mode",
    "not allowed",
)

# server address -> bool | None (None = not probed yet)
_control_capability = {}


def discover_servers() -> list:
    """Every Triton replica we should talk to."""
    if settings.triton_discovery == "static":
        servers = []
        for endpoint in settings.triton_endpoints:
            address = (
                endpoint
                if ":" in endpoint
                else f"{endpoint}:{settings.triton_http_port}"
            )
            servers.append(
                {
                    "name": address,
                    "address": address,
                    "url": f"http://{address}",
                    "node": None,
                    "ready": None,
                    "phase": None,
                }
            )
        return servers

    servers = []
    for pod in kube.list_triton_pods():
        address = f"{pod['ip']}:{settings.triton_http_port}"
        servers.append(
            {
                "name": pod["name"],
                "address": address,
                "url": f"http://{address}",
                "node": pod["node"],
                "ready": pod["ready"],
                "phase": pod["phase"],
            }
        )
    return servers


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=settings.triton_timeout_s)


async def _repository_index(client: httpx.AsyncClient, server: dict) -> dict:
    """POST /v2/repository/index for one server."""
    out = dict(server)
    out["models"] = []
    out["error"] = None
    out["live"] = False
    try:
        response = await client.post(f"{server['url']}/v2/repository/index", json={})
        response.raise_for_status()
        payload = response.json()
        out["live"] = True
        out["models"] = [
            {
                "name": item.get("name", ""),
                "version": item.get("version", ""),
                "state": item.get("state", "UNKNOWN"),
                "reason": item.get("reason", ""),
            }
            for item in payload
            if item.get("name")
        ]
    except Exception as exc:
        out["error"] = _short_error(exc)
    out["controlEnabled"] = _control_capability.get(server["address"])
    return out


def _short_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
    return f"{type(exc).__name__}: {exc}"[:300]


async def collect_state() -> dict:
    """Query every server's repository index concurrently."""
    servers = discover_servers()
    if not servers:
        return {"servers": [], "models": {}}

    async with _client() as client:
        results = await asyncio.gather(
            *(_repository_index(client, server) for server in servers)
        )

    # model name -> {server name -> {state, version, reason}}
    models = {}
    for server in results:
        for entry in server["models"]:
            models.setdefault(entry["name"], {})[server["name"]] = {
                "state": entry["state"],
                "version": entry["version"],
                "reason": entry["reason"],
            }
    return {"servers": results, "models": models}


async def _control_one(
    client: httpx.AsyncClient, server: dict, model: str, action: str
) -> dict:
    url = f"{server['url']}/v2/repository/models/{model}/{action}"
    try:
        response = await client.post(url, json={})
        if response.status_code >= 400:
            message = response.text[:400]
            if any(hint in message.lower() for hint in _EXPLICIT_HINTS):
                _control_capability[server["address"]] = False
                return {
                    "server": server["name"],
                    "ok": False,
                    "error": "Triton was not started with --model-control-mode=explicit, "
                    "so models cannot be loaded or unloaded at runtime.",
                    "controlDisabled": True,
                }
            return {
                "server": server["name"],
                "ok": False,
                "error": f"HTTP {response.status_code}: {message}",
            }
        _control_capability[server["address"]] = True
        return {"server": server["name"], "ok": True, "error": None}
    except Exception as exc:
        return {"server": server["name"], "ok": False, "error": _short_error(exc)}


async def control_model(model: str, action: str, server_names=None) -> dict:
    """Load or unload ``model`` on all (or the named) servers."""
    if action not in ("load", "unload"):
        raise ValueError(f"Unsupported action: {action}")

    servers = discover_servers()
    if server_names:
        wanted = set(server_names)
        servers = [s for s in servers if s["name"] in wanted or s["address"] in wanted]
    if not servers:
        return {
            "action": action,
            "model": model,
            "results": [],
            "ok": False,
            "error": "No Triton servers found.",
        }

    # Unloading uses a longer budget: Triton drains in-flight requests first.
    timeout = settings.triton_timeout_s * (6 if action == "unload" else 3)
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = await asyncio.gather(
            *(_control_one(client, server, model, action) for server in servers)
        )

    ok = all(r["ok"] for r in results)
    return {
        "action": action,
        "model": model,
        "results": results,
        "ok": ok,
        "error": None if ok else "; ".join(r["error"] for r in results if r["error"]),
    }
