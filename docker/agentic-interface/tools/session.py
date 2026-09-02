"""AF session management — start, stop, and inspect the user's JupyterHub server.

All calls go through the JupyterHub REST API using the user's own token, so they
are automatically scoped to that user's server.

Failure reporting follows errors.py: every Hub answer that is not what a tool
needed is translated into what it means for *this* user — a token that has
just been revoked, a token that may not manage sessions, a hub that is down —
and no state is ever inferred from a call that did not succeed.
"""

import asyncio
import time
from typing import Any, Optional

import httpx
from auth import clear_user_cache
from config import HUB_API_URL, PUBLIC_URL, TOKEN_URL
from context import require_user
from errors import (
    AuthError,
    Failure,
    UpstreamError,
    UserError,
    describe_exception,
    http_error,
    json_body,
    malformed_response,
    response_detail,
    unreachable,
)
from mcp.server.fastmcp import Context
from shared import shared_client

from tools.elicitation import ask, single_choice_model
from tools.gpu import apply_availability, free_gpus


def _auth(token: str) -> dict:
    return {"Authorization": f"token {token}"}


# ── failure reporting ─────────────────────────────────────────────────────────


def _hub_error(resp: httpx.Response, action: str, username: str) -> Failure:
    """What a non-2xx Hub answer means for this user, and what to do next.

    The token passed the service's own check moments earlier, so a 401 here
    means it has just been revoked — inside a session that is what a restart
    does. A 403 is the other in-session case: a session's own token may read
    its session but not manage it.
    """
    code = resp.status_code
    if code == 401:
        return AuthError(
            f"Error: JupyterHub rejected this token (HTTP 401) while trying to "
            f"{action}. It was accepted when this request was authenticated, so "
            "it has just expired or been revoked — inside an AF session the token "
            "changes on every restart, so restart the agent so it picks up the "
            "new JUPYTERHUB_API_TOKEN; from your own machine, mint a new token "
            f"at {TOKEN_URL} and reconnect the MCP server."
        )
    if code == 403:
        detail = response_detail(resp, limit=160)
        return AuthError(
            f"Error: this token is not permitted to {action} (JupyterHub HTTP 403"
            + (f": {detail}" if detail else "")
            + "). Tokens issued to a running session can only read their own "
            "session; to start, stop, or restart sessions use a token created "
            f"at {TOKEN_URL} from outside the session."
        )
    if code == 404:
        return AuthError(
            f"Error: JupyterHub has no user '{username}' (HTTP 404) while trying "
            f"to {action} — the account may have been removed from the hub. "
            "Contact AF support."
        )
    return http_error("JupyterHub API", resp, action=action)


async def _hub(
    method: str,
    path: str,
    *,
    token: str,
    username: str,
    action: str,
    ok: tuple[int, ...] = (200,),
    timeout: float = 10.0,
    json: Any = None,
) -> httpx.Response:
    """One Hub API call — or the Failure that explains why it could not be
    made. ``ok`` lists the statuses the caller handles itself."""
    try:
        resp = await shared_client("hub").request(
            method,
            f"{HUB_API_URL}{path}",
            headers=_auth(token),
            json=json,
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        raise unreachable(
            "JupyterHub API",
            exc,
            next_step=f"The request to {action} was not made. Try again in a "
            "minute; if the hub stays unreachable, get_facility_health shows "
            "whether the facility is down.",
        )
    if resp.status_code not in ok:
        raise _hub_error(resp, action, username)
    return resp


_CANNOT_SEE_SERVERS = (
    "Error: cannot read session state for '{username}': this token is not "
    "permitted to list servers, so I cannot tell whether a session is running. "
    "This is a permissions gap, not a stopped session — inside an AF session it "
    "means the session image predates the fix (restart the session); from your "
    "own machine, mint a fresh token at {token_url}. The JupyterHub interface "
    "at {public_url}/hub/home always shows the real state."
)


def _cannot_see_servers(username: str) -> AuthError:
    return AuthError(
        _CANNOT_SEE_SERVERS.format(
            username=username, token_url=TOKEN_URL, public_url=PUBLIC_URL
        )
    )


# ── profile / option selection helpers ────────────────────────────────────────

# Returned whenever an interactive choice couldn't be collected — either the
# client can't render elicitation, or the prompt was dismissed/cancelled (which
# also happens when the server→client stream is flaky). Rather than dead-ending,
# hand the agent everything it needs to ask the user in chat and retry, so the
# session can always be started.
_ELICIT_FALLBACK = (
    "Couldn't collect the choices interactively. Ask the user which profile and "
    "options they want — call list_af_profiles for the exact slugs, option keys, "
    "and live GPU availability — then call start_af_session again with "
    "profile_name and user_options. To skip the questions entirely, call "
    "start_af_session(use_defaults=True) to launch the default profile."
)


def _default_profile(profiles: list[dict]) -> Optional[dict]:
    """Return the profile marked default, else the first, else None."""
    for p in profiles:
        if p.get("default"):
            return p
    return profiles[0] if profiles else None


def _default_choice_key(choices: dict[str, str]) -> Optional[str]:
    """Pick the choice key whose label is marked '(default)', else the first."""
    for key, label in choices.items():
        if str(label).rstrip().endswith("(default)"):
            return key
    return next(iter(choices), None)


def _servers(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return the user's servers, or None when the token may not see them.

    The Hub omits `servers` entirely from a user model when the token lacks
    `read:servers` — it does NOT return an empty dict. Singleuser server tokens
    (what an agent running inside a session carries) are exactly that case, so
    treating a missing key as "no session" tells a user their running session
    does not exist. Absent means unknown; only an empty dict means none.
    """
    servers = data.get("servers")
    if servers is None:
        return None
    return servers if isinstance(servers, dict) else {}


def register(mcp: Any) -> None:
    @mcp.tool()
    async def get_session_status() -> str:
        """Return the current status of the user's Analysis Facility session (pod).

        Includes: whether it is running, which profile and resources were selected,
        how long it has been active, and the URL to access it.
        """
        user = require_user()
        token = user["token"]
        username = user["username"]

        resp = await _hub(
            "GET",
            f"/users/{username}",
            token=token,
            username=username,
            action="read the session state",
        )
        data = json_body(resp)
        if not isinstance(data, dict):
            raise malformed_response("JupyterHub API", resp, "a user record")
        servers = _servers(data)
        if servers is None:
            raise _cannot_see_servers(username)

        base = f"{PUBLIC_URL}/user/{username}"
        lab_url = f"{base}/lab"
        vscode_url = f"{base}/vscode/?folder=/home/{username}"

        if not servers:
            return "\n".join(
                [
                    f"No active session for user '{username}'.",
                    "Use start_af_session to launch one.",
                    "",
                    "Interface links (will redirect to spawn form until a session is running):",
                    f"  JupyterLab  {lab_url}",
                    f"  VS Code     {vscode_url}",
                ]
            )

        default = servers.get("", {})
        ready = default.get("ready", False)
        pending = default.get("pending")
        started = default.get("started", "")
        user_options = default.get("user_options", {})
        # Do not read servers[""].state — that field requires admin:server_state.

        status_str = (
            "running" if ready else f"pending ({pending})" if pending else "not ready"
        )

        # Determine the active interface from user_options. The option key is
        # numbered per profile ("3-interface" today, but renumbering happens),
        # so scan for the first key that is "interface" or ends with
        # "-interface". Choice "2" means VS Code, "1" (or absent) JupyterLab.
        interface_choice = next(
            (
                v
                for k, v in user_options.items()
                if k == "interface" or k.endswith("-interface")
            ),
            "1",
        )
        vscode_active = interface_choice == "2"

        lines = [
            f"# Session status: {status_str}",
            f"user: {username}",
        ]
        if started:
            lines.append(f"started: {started}")

        # Always include both interface links so the user can open either at any time.
        # Mark which one was selected at spawn time (or JupyterLab if unspecified).
        lines.append("\nInterface links:")
        lines.append(
            f"  JupyterLab  {lab_url}" + ("" if vscode_active else "  ← active")
        )
        lines.append(
            f"  VS Code     {vscode_url}" + ("  ← active" if vscode_active else "")
        )

        if user_options:
            lines.append("\nSelected options:")
            for k, v in user_options.items():
                lines.append(f"  {k}: {v}")

        return "\n".join(lines)

    @mcp.tool()
    async def start_af_session(
        ctx: Context,
        profile_name: Optional[str] = None,
        user_options: Optional[dict] = None,
        use_defaults: bool = False,
    ) -> str:
        """Start the user's Analysis Facility session (JupyterHub pod).

        When ``profile_name`` / ``user_options`` are omitted (and use_defaults is
        False), the tool asks the user via the client's multiple-choice UI (MCP
        elicitation): first the profile, then one question per configurable
        option (interface, CPU, memory, …), each defaulting to the profile's
        default. Clients that can't elicit get a short instruction instead.

        Pass ``use_defaults=True`` to skip all questions and launch the default
        profile immediately. If a session is already running this is a no-op.

        Args:
            profile_name: Profile slug or display name from list_af_profiles.
                          Elicited from the user if omitted.
            user_options: Dict of option_key → choice_value as listed by
                          list_af_profiles.  Example for the stable profile:
                          {"3-interface": "2", "0-cpu": "3", "2-memory": "2"}.
                          Any option not supplied is elicited.
            use_defaults: Skip elicitation and start the default profile/options.
        """
        user = require_user()
        token = user["token"]
        username = user["username"]

        opts: dict = dict(user_options or {})

        from tools.profiles import find_profile, get_profiles, profiles_error

        profiles = await get_profiles()

        # ── Resolve which profile to launch ──
        selected: Optional[dict] = None
        if profile_name:
            selected = find_profile(profiles, profile_name)
            if selected is None:
                if not profiles:
                    raise UpstreamError(
                        f"Error: cannot check profile '{profile_name}' — the "
                        "profile list could not be read "
                        f"({profiles_error() or 'unknown reason'}). Try again in "
                        "a minute, or call start_af_session(use_defaults=True) "
                        "to launch the Hub's default profile."
                    )
                known = ", ".join(f'"{p["slug"]}"' for p in profiles)
                raise UserError(
                    f"Unknown profile '{profile_name}'. "
                    f"Call list_af_profiles to see available options. "
                    f"Known slugs: {known}"
                )
        elif use_defaults:
            selected = _default_profile(profiles)
        elif len(profiles) == 1:
            # Only ask when there is a genuine choice to make.
            selected = profiles[0]
        elif profiles:
            model = single_choice_model(
                "ProfileChoice",
                [p["slug"] for p in profiles],
                labels=[
                    p["display_name"] + (" (default)" if p.get("default") else "")
                    for p in profiles
                ],
                default=(_default_profile(profiles) or {}).get("slug"),
                description="Analysis Facility session profile.",
                field="profile",
            )
            choice = await ask(
                ctx, "Choose a session profile.", model, _ELICIT_FALLBACK
            )
            selected = find_profile(profiles, choice.profile)
        # profiles empty (e.g. local/dev) → selected stays None → Hub default.

        if selected is not None:
            # KubeSpawner selects the profile by slug; always include it.
            opts["profile"] = selected["slug"]

            # ── Ask for each option the caller didn't already specify ──
            if not use_defaults:
                for opt_key, opt_info in selected.get("options", {}).items():
                    if opt_key in opts:
                        continue
                    choices = opt_info.get("choices") or {}
                    if not choices:
                        continue
                    # GPU options: annotate with live availability and hide any
                    # flavor that has none free right now (same data the Hub form
                    # uses). Non-GPU options are unchanged.
                    gpu_map = opt_info.get("gpu")
                    if gpu_map:
                        labels_map, keys = apply_availability(
                            choices, gpu_map, await free_gpus()
                        )
                    else:
                        labels_map, keys = choices, list(choices)
                    model = single_choice_model(
                        f"Option_{len(opts)}",
                        keys,
                        labels=[labels_map[k] for k in keys],
                        default=_default_choice_key(choices),
                        description=opt_info.get("display_name", opt_key),
                    )
                    choice = await ask(
                        ctx,
                        f"{opt_info.get('display_name', opt_key)}:",
                        model,
                        _ELICIT_FALLBACK,
                    )
                    opts[opt_key] = choice.value

        resp = await _hub(
            "POST",
            f"/users/{username}/server",
            token=token,
            username=username,
            action="start the session",
            ok=(200, 201, 202, 400),
            timeout=15.0,
            json=opts,
        )

        if resp.status_code == 400:
            # 400 is most commonly "already running", but JupyterHub also uses it
            # for rejected spawn options — don't mask those behind a success message.
            body = resp.text.lower()
            if "already running" in body or "already pending" in body:
                return (
                    "Session is already running. Use get_session_status to see its URL."
                )
            raise UserError(
                "Error: JupyterHub rejected the spawn request — "
                f"{response_detail(resp) or 'no reason given'}. Check the profile "
                "and options against list_af_profiles and call start_af_session "
                "again."
            )

        clear_user_cache(token)

        note = ""
        if selected is None and not profiles:
            note = (
                "\nNote: the profile list could not be read "
                f"({profiles_error() or 'unknown reason'}), so the Hub's default "
                "profile and options were used."
            )

        if resp.status_code == 201:
            return (
                "Session is starting. This typically takes 30–60 seconds. "
                "Call wait_for_session to block until it is ready." + note
            )
        if resp.status_code == 202:
            return (
                "Session start accepted — a server is already pending. "
                "Use get_session_status to check progress." + note
            )
        return "Session starting. Use get_session_status to check progress." + note

    @mcp.tool()
    async def stop_af_session() -> str:
        """Stop the user's running Analysis Facility session (JupyterHub pod).

        Any unsaved notebook state and running kernels will be lost.
        Storage (home directory, /work) is preserved.
        """
        user = require_user()
        token = user["token"]
        username = user["username"]

        resp = await _hub(
            "DELETE",
            f"/users/{username}/server",
            token=token,
            username=username,
            action="stop the session",
            ok=(200, 202, 204, 400),
            timeout=15.0,
        )
        if resp.status_code == 400:
            return "No session is currently running."

        clear_user_cache(token)
        return (
            "Session is stopping. Storage is preserved — "
            "use start_af_session to launch a new one."
        )

    @mcp.tool()
    async def wait_for_session(timeout_seconds: int = 180) -> str:
        """Poll until the user's session is fully running or timeout is reached.

        Use this immediately after start_af_session instead of manually calling
        get_session_status in a loop.  Polls the JupyterHub API every 10 seconds
        internally and returns as soon as the pod is ready — or as soon as it is
        clear the session will not become ready (no spawn in progress, the
        session is stopping, or the token can no longer see it).

        Args:
            timeout_seconds: Maximum time to wait. Default: 180 s (3 min).
                             Clamped to the 1–600 s range — values outside it
                             are adjusted rather than rejected.
        """
        user = require_user()
        token = user["token"]
        username = user["username"]

        # Clamp rather than error: this call holds the request open for the
        # whole wait, so an unbounded timeout would pin a connection.
        timeout_seconds = max(1, min(timeout_seconds, 600))
        deadline = time.monotonic() + timeout_seconds
        poll_interval = 10
        attempts = 0
        failed_polls = 0
        absent_polls = 0
        last_seen = "no status check completed"

        client = shared_client("hub")
        while True:
            attempts += 1
            try:
                resp = await client.get(
                    f"{HUB_API_URL}/users/{username}",
                    headers=_auth(token),
                    timeout=10.0,
                )
            except httpx.RequestError as exc:
                # Transient network trouble: keep polling, but remember it so a
                # timeout can say the hub was unreachable rather than "not ready".
                failed_polls += 1
                last_seen = f"JupyterHub API unreachable — {describe_exception(exc)}"
            else:
                if resp.status_code in (401, 403, 404):
                    # No amount of waiting changes what the token may see.
                    raise _hub_error(resp, "check the session state", username)
                if resp.status_code != 200:
                    failed_polls += 1
                    last_seen = f"JupyterHub API returned HTTP {resp.status_code}"
                else:
                    data = json_body(resp)
                    servers = _servers(data if isinstance(data, dict) else {})
                    if servers is None:
                        raise _cannot_see_servers(username)
                    default = servers.get("")
                    if default is None:
                        # A spawn in progress is always listed (pending="spawn");
                        # nothing listed means no spawn was accepted, or it has
                        # already failed. Two looks rule out a momentary gap.
                        absent_polls += 1
                        if absent_polls >= 2:
                            raise UserError(
                                "Error: no session is starting or running — "
                                f"JupyterHub lists no server for '{username}', "
                                "so either start_af_session was not called or the "
                                "spawn failed. Call start_af_session again; if it "
                                "fails again, the spawn page at "
                                f"{PUBLIC_URL}/hub/home shows JupyterHub's reason."
                            )
                        last_seen = "no server listed"
                    else:
                        absent_polls = 0
                        if default.get("ready", False):
                            started = default.get("started", "")
                            clear_user_cache(token)
                            lines = [
                                "Session is running.",
                                f"(became ready after {attempts} poll(s))",
                            ]
                            if started:
                                lines.insert(1, f"started: {started}")
                            lines += [
                                "",
                                "Next: get_session_status returns browser links.",
                            ]
                            return "\n".join(lines)
                        pending = default.get("pending")
                        if pending == "stop":
                            raise UserError(
                                "Error: the session is stopping, not starting. "
                                "Wait for get_session_status to report no active "
                                "session, then call start_af_session."
                            )
                        last_seen = (
                            f"session pending ({pending})"
                            if pending
                            else "session listed but not ready"
                        )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))

        lines = [
            f"Session did not become ready within {timeout_seconds} s.",
            f"Last observed: {last_seen}.",
        ]
        if failed_polls:
            lines.append(
                f"{failed_polls} of {attempts} status check(s) failed to reach "
                "JupyterHub, so the session may be further along than this shows."
            )
        lines.append(
            "Next: get_session_status for the current state; if it stays pending, "
            "query_notebook_logs shows the pod's startup log, and "
            "get_facility_health shows whether the facility is short of capacity."
        )
        return "\n".join(lines)

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
        user = require_user()
        token = user["token"]
        username = user["username"]

        # 1. Capture current user_options before stopping. A hub that cannot
        #    answer is not fatal — but it is reported, because "same options
        #    as before" silently becoming "default options" is a surprise.
        #    A token that may not read the state is fatal: nothing else works.
        prior_opts: dict = {}
        prior_note = ""
        try:
            info = await _hub(
                "GET",
                f"/users/{username}",
                token=token,
                username=username,
                action="read the session state",
            )
        except UpstreamError as exc:
            prior_note = f" (the previous options could not be read: {exc})"
        else:
            data = json_body(info)
            servers = _servers(data if isinstance(data, dict) else {}) or {}
            prior_opts = servers.get("", {}).get("user_options", {}) or {}

        # 2. Decide on spawn options: caller overrides take precedence over prior state.
        spawn_opts: dict = dict(user_options or prior_opts)
        if profile_name:
            from tools.profiles import find_profile, get_profiles, profiles_error

            profiles = await get_profiles()
            profile = find_profile(profiles, profile_name)
            if profile is None:
                known = (
                    ", ".join(f'"{p["slug"]}"' for p in profiles)
                    if profiles
                    else f"unavailable ({profiles_error() or 'unknown reason'})"
                )
                raise UserError(
                    f"Unknown profile '{profile_name}'. "
                    f"Call list_af_profiles to see options. "
                    f"Known slugs: {known}"
                )
            spawn_opts["profile"] = profile["slug"]

        # 3. Stop.
        stop = await _hub(
            "DELETE",
            f"/users/{username}/server",
            token=token,
            username=username,
            action="stop the session",
            ok=(200, 202, 204, 400),
            timeout=15.0,
        )
        was_running = stop.status_code != 400  # 400 = no server was running
        if was_running:
            # The old pod is gone — invalidate cached user info so tools
            # don't target a terminated pod for up to a cache TTL.
            clear_user_cache(token)
            # 4. Brief pause to let Kubernetes terminate the pod before re-spawning.
            await asyncio.sleep(3)

        # 5. Start.
        try:
            start = await _hub(
                "POST",
                f"/users/{username}/server",
                token=token,
                username=username,
                action="start the session",
                ok=(200, 201, 202, 400),
                timeout=15.0,
                json=spawn_opts,
            )
        except Failure as exc:
            raise type(exc)(
                f"Session was stopped, but the restart failed. {exc} "
                "Use start_af_session to try again."
            )

        if start.status_code == 400:
            body = start.text.lower()
            if any(w in body for w in ("pending", "running", "stop", "terminat")):
                # Pod still terminating — ask the user to retry
                raise UpstreamError(
                    "Session stopped but the pod is still terminating. "
                    "Wait a few seconds then call start_af_session to complete the restart."
                )
            raise UserError(
                "Session was stopped, but JupyterHub rejected the restart options — "
                f"{response_detail(start) or 'no reason given'}. Call "
                "start_af_session with valid options (see list_af_profiles)."
            )

        opts_summary = (
            ", ".join(f"{k}={v}" for k, v in spawn_opts.items())
            if spawn_opts
            else "default options"
        )
        return (
            f"Session restarting with {opts_summary}{prior_note}. "
            "Call wait_for_session to block until ready."
        )
