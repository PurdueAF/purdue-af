"""AF session management — start, stop, and inspect the user's JupyterHub server.

All calls go through the JupyterHub REST API using the user's own token, so they
are automatically scoped to that user's server.
"""

import os
from typing import Optional

import httpx

from context import current_user

HUB_API_URL = os.environ.get("JUPYTERHUB_API_URL", "http://hub:8081/hub/api")


def _auth(token: str) -> dict:
    return {"Authorization": f"token {token}"}


def register(mcp) -> None:
    @mcp.tool()
    async def get_session_status() -> str:
        """Return the current status of the user's Analysis Facility session (pod).

        Includes: whether it is running, which profile and resources were selected,
        how long it has been active, and the URL to access it.
        """
        user = current_user.get()
        token = user["token"]
        username = user["username"]

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    f"{HUB_API_URL}/users/{username}",
                    headers=_auth(token),
                    timeout=10.0,
                )
            except httpx.RequestError as exc:
                return f"Error: JupyterHub API unreachable — {exc}"

        if resp.status_code != 200:
            return f"Error: JupyterHub API returned HTTP {resp.status_code}"

        data = resp.json()
        servers = data.get("servers", {})

        if not servers:
            return f"No active session for user '{username}'. Use start_af_session to launch one."

        default = servers.get("", {})
        ready = default.get("ready", False)
        pending = default.get("pending")
        started = default.get("started", "")
        url = default.get("url", "")
        user_options = default.get("user_options", {})
        state = default.get("state", {})
        pod_name = state.get("pod_name", "")

        status_str = "running" if ready else f"pending ({pending})" if pending else "not ready"

        lines = [
            f"# Session status: {status_str}",
            f"user: {username}",
        ]
        if pod_name:
            lines.append(f"pod: {pod_name}")
        if started:
            lines.append(f"started: {started}")
        if url:
            lines.append(f"url: https://cms.geddes.rcac.purdue.edu{url}")
        if user_options:
            lines.append("\nSelected options:")
            for k, v in user_options.items():
                lines.append(f"  {k}: {v}")

        return "\n".join(lines)

    @mcp.tool()
    async def start_af_session(
        profile: Optional[str] = None,
        interface: Optional[str] = None,
        gpu: Optional[str] = None,
    ) -> str:
        """Start the user's Analysis Facility session (JupyterHub pod).

        If a session is already running, this is a no-op and returns its current URL.

        Args:
            profile: Profile to launch. Options: 'stable' (default), 'pre-release'.
                     The pre-release profile includes the AI sidecar.
            interface: 'lab' for JupyterLab (default) or 'vscode' for VS Code.
            gpu: GPU allocation — '0' (none, default), '1_mig' (5 GB A100 slice),
                 '1_a100' (full 40 GB A100, limited availability).
        """
        user = current_user.get()
        token = user["token"]
        username = user["username"]

        # Map friendly names to JupyterHub profile slugs
        _profile_map = {
            "stable": "",                           # default profile
            "pre-release": "latest-pre-release-version",
        }
        _interface_map = {"lab": "1", "vscode": "2"}
        _gpu_map = {"0": "1", "1_mig": "2", "1_a100": "3"}

        user_options: dict = {}
        if profile:
            slug = _profile_map.get(profile.lower(), profile)
            if slug:
                user_options["profile"] = slug
        if interface:
            user_options["interface"] = _interface_map.get(interface.lower(), interface)
        if gpu:
            user_options["gpu"] = _gpu_map.get(gpu.lower(), gpu)

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{HUB_API_URL}/users/{username}/server",
                    headers=_auth(token),
                    json=user_options if user_options else {},
                    timeout=15.0,
                )
            except httpx.RequestError as exc:
                return f"Error: JupyterHub API unreachable — {exc}"

        if resp.status_code == 400:
            return "Session is already running. Use get_session_status to see its URL."
        if resp.status_code == 201:
            return (
                "Session is starting. This typically takes 30–60 seconds. "
                "Use get_session_status to check progress."
            )
        if resp.status_code == 202:
            return (
                "Session start accepted — a server is already pending. "
                "Use get_session_status to check progress."
            )
        if resp.status_code not in (200, 201, 202):
            return f"Error: JupyterHub returned HTTP {resp.status_code} — {resp.text[:300]}"

        return "Session starting. Use get_session_status to check progress."

    @mcp.tool()
    async def stop_af_session() -> str:
        """Stop the user's running Analysis Facility session (JupyterHub pod).

        Any unsaved notebook state and running kernels will be lost.
        Storage (home directory, /work) is preserved.
        """
        user = current_user.get()
        token = user["token"]
        username = user["username"]

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.delete(
                    f"{HUB_API_URL}/users/{username}/server",
                    headers=_auth(token),
                    timeout=15.0,
                )
            except httpx.RequestError as exc:
                return f"Error: JupyterHub API unreachable — {exc}"

        if resp.status_code == 400:
            return "No session is currently running."
        if resp.status_code not in (200, 202, 204):
            return f"Error: JupyterHub returned HTTP {resp.status_code} — {resp.text[:300]}"

        return (
            "Session is stopping. Storage is preserved — "
            "use start_af_session to launch a new one."
        )
