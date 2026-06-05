"""AF session management — start, stop, and inspect the user's JupyterHub server.

All calls go through the JupyterHub REST API using the user's own token, so they
are automatically scoped to that user's server.
"""

import asyncio
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

        status_str = (
            "running" if ready else f"pending ({pending})" if pending else "not ready"
        )

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
        profile_name: Optional[str] = None,
        user_options: Optional[dict] = None,
    ) -> str:
        """Start the user's Analysis Facility session (JupyterHub pod).

        Call list_af_profiles first to discover valid profile slugs and option
        key/value pairs.  If a session is already running this is a no-op.

        Args:
            profile_name: Profile slug or display name from list_af_profiles.
                          Omit to use the default profile.
            user_options: Dict of option_key → choice_value as listed by
                          list_af_profiles.  Example for the stable profile:
                          {"3-interface": "2", "0-cpu": "3", "2-memory": "2"}
        """
        user = current_user.get()
        token = user["token"]
        username = user["username"]

        opts: dict = dict(user_options or {})

        if profile_name:
            from tools.profiles import find_profile, get_profiles

            profiles = await get_profiles()
            profile = find_profile(profiles, profile_name)
            if profile is None:
                known = (
                    ", ".join(f'"{p["slug"]}"' for p in profiles)
                    if profiles
                    else "unavailable"
                )
                return (
                    f"Unknown profile '{profile_name}'. "
                    f"Call list_af_profiles to see available options. "
                    f"Known slugs: {known}"
                )
            # Add the profile key so KubeSpawner selects the right profile.
            # For the default profile the slug is still non-empty (e.g.
            # "purdue-af-0-12-4-…"), so we always include it when explicitly requested.
            opts["profile"] = profile["slug"]

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{HUB_API_URL}/users/{username}/server",
                    headers=_auth(token),
                    json=opts,
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

    @mcp.tool()
    async def restart_af_session(
        profile_name: Optional[str] = None,
        user_options: Optional[dict] = None,
    ) -> str:
        """Restart the Analysis Facility session.

        By default restarts with the same profile and resource options that were
        active before the stop.  Pass profile_name / user_options to switch to
        different settings on restart.

        Args:
            profile_name: Profile slug or display name from list_af_profiles.
                          Omit to keep the current profile.
            user_options: Option overrides.  Omit to reuse the current options.
                          See list_af_profiles for valid keys and values.
        """
        user = current_user.get()
        token = user["token"]
        username = user["username"]

        async with httpx.AsyncClient() as client:
            # 1. Capture current user_options before stopping.
            prior_opts: dict = {}
            try:
                info = await client.get(
                    f"{HUB_API_URL}/users/{username}",
                    headers=_auth(token),
                    timeout=10.0,
                )
                if info.status_code == 200:
                    prior_opts = (
                        info.json()
                        .get("servers", {})
                        .get("", {})
                        .get("user_options", {})
                    )
            except httpx.RequestError:
                pass  # non-fatal — we'll restart with whatever options we have

            # 2. Decide on spawn options: caller overrides take precedence over prior state.
            spawn_opts: dict = dict(user_options or prior_opts)
            if profile_name:
                from tools.profiles import find_profile, get_profiles

                profiles = await get_profiles()
                profile = find_profile(profiles, profile_name)
                if profile is None:
                    known = (
                        ", ".join(f'"{p["slug"]}"' for p in profiles)
                        if profiles
                        else "unavailable"
                    )
                    return (
                        f"Unknown profile '{profile_name}'. "
                        f"Call list_af_profiles to see options. "
                        f"Known slugs: {known}"
                    )
                spawn_opts["profile"] = profile["slug"]

            # 3. Stop.
            try:
                stop = await client.delete(
                    f"{HUB_API_URL}/users/{username}/server",
                    headers=_auth(token),
                    timeout=15.0,
                )
            except httpx.RequestError as exc:
                return f"Error: JupyterHub API unreachable — {exc}"

            if stop.status_code not in (200, 202, 204, 400):
                return f"Error stopping session: HTTP {stop.status_code} — {stop.text[:300]}"

            was_running = stop.status_code != 400  # 400 = no server was running

            # 4. Brief pause to let Kubernetes terminate the pod before re-spawning.
            if was_running:
                await asyncio.sleep(3)

            # 5. Start.
            try:
                start = await client.post(
                    f"{HUB_API_URL}/users/{username}/server",
                    headers=_auth(token),
                    json=spawn_opts,
                    timeout=15.0,
                )
            except httpx.RequestError as exc:
                return (
                    f"Session was stopped but restart failed: JupyterHub API unreachable — {exc}. "
                    "Use start_af_session to try again."
                )

        if start.status_code == 400:
            # Pod still terminating — ask the user to retry
            return (
                "Session stopped but the pod is still terminating. "
                "Wait a few seconds then call start_af_session to complete the restart."
            )
        if start.status_code not in (200, 201, 202):
            return (
                f"Session stopped but restart returned HTTP {start.status_code}. "
                "Use start_af_session to try again."
            )

        opts_summary = (
            ", ".join(f"{k}={v}" for k, v in spawn_opts.items())
            if spawn_opts
            else "default options"
        )
        return (
            f"Session restarting with {opts_summary}. "
            "Use get_session_status to check progress."
        )
