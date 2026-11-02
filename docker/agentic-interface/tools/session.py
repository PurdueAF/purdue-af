"""AF session management — start, stop, and inspect the user's JupyterHub server.

All calls go through the JupyterHub REST API using the user's own token, so they
are automatically scoped to that user's server. No state is ever inferred from
a call that did not succeed.
"""

import asyncio
import time
from typing import Any, NamedTuple, Optional

import httpx
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
from mcp.server.mcpserver import Context, MCPServer
from shared import shared_client

from tools import profiles
from tools.elicitation import ask, single_choice_model
from tools.gpu import apply_availability, free_gpus

_SERVICE = "JupyterHub API"
_POLL_INTERVAL = 10
# Bounds restart's wait for a stop: the call holds its request open meanwhile.
_STOP_WAIT = 90.0


def _auth(token: str) -> dict:
    return {"Authorization": f"token {token}"}


# ── failure reporting ─────────────────────────────────────────────────────────


def _hub_error(resp: httpx.Response, action: str, username: str) -> Failure:
    """What a non-2xx Hub answer means for this user, and what to do next.

    The token passed the service's own check moments earlier, so a 401 means it
    has just been revoked: inside a session a restart does that. A 403 is a
    session's own token, which may read its session but not manage it.
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
    return http_error(_SERVICE, resp, action=action)


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
            _SERVICE,
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
    "This is a permissions gap, not a stopped session: this token lacks "
    "read:servers. Inside an AF session, a restarted session gets a token that "
    "has it; from your own machine, mint a fresh token at {token_url}. The "
    "JupyterHub interface at {public_url}/hub/home always shows the real state."
)
_STILL_STOPPING = (
    "Error: the session is still stopping, so it cannot start yet. Wait for "
    "get_session_status to report no active session, then call start_af_session."
)


def _cannot_see_servers(username: str) -> AuthError:
    return AuthError(
        _CANNOT_SEE_SERVERS.format(
            username=username, token_url=TOKEN_URL, public_url=PUBLIC_URL
        )
    )


def _servers(resp: httpx.Response, username: str) -> dict[str, Any]:
    """The servers in a GET /users/{name} answer; only an empty dict means none.

    The Hub omits `servers` from a user model when the token lacks
    `read:servers`, as a session's own token can, so absent means unknown.
    """
    data = json_body(resp)
    if isinstance(data, dict) and data.get("servers") is None:
        raise _cannot_see_servers(username)
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise malformed_response(_SERVICE, resp, "a user record")
    return servers


async def _user_servers(token: str, username: str) -> dict[str, Any]:
    resp = await _hub(
        "GET",
        f"/users/{username}",
        token=token,
        username=username,
        action="read the session state",
    )
    return _servers(resp, username)


async def _stop(token: str, username: str) -> int:
    """Ask the Hub to stop the server; its status says how that went: 400 when
    no server was running, 202 while the stop is still in progress."""
    resp = await _hub(
        "DELETE",
        f"/users/{username}/server",
        token=token,
        username=username,
        action="stop the session",
        ok=(200, 202, 204, 400),
        timeout=15.0,
    )
    return resp.status_code


async def _spawn(token: str, username: str, opts: dict) -> httpx.Response:
    return await _hub(
        "POST",
        f"/users/{username}/server",
        token=token,
        username=username,
        action="start the session",
        ok=(200, 201, 202, 400),
        timeout=15.0,
        json=opts,
    )


def _spawn_refusal(resp: httpx.Response) -> str:
    """Why the Hub answered a spawn with 400, from its own texts: "running",
    "stopping", or "rejected" for the options."""
    if "is already running" in resp.text:
        return "running"
    if "is pending stop" in resp.text:
        return "stopping"
    return "rejected"


async def _await_stop(token: str, username: str) -> None:
    """Wait, bounded, for a stop still in progress; the spawn after it reports
    a stop that outlasts the wait."""
    deadline = time.monotonic() + _STOP_WAIT
    while True:
        try:
            server = (await _user_servers(token, username)).get("")
            if server is None or server.get("pending") != "stop":
                return
        except UpstreamError:
            pass  # keep polling: a backend blip says nothing about the stop
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(_POLL_INTERVAL, remaining))


class _Poll(NamedTuple):
    """What one status check of a wait found."""

    seen: str
    ready: bool = False
    started: str = ""
    failed: bool = False
    absent: bool = False


def _classify_poll(resp: httpx.Response, username: str) -> _Poll:
    """What a status answer means for a wait; raises when waiting cannot help."""
    if resp.status_code in (401, 403, 404):
        raise _hub_error(resp, "check the session state", username)
    if resp.status_code != 200:
        return _Poll(f"{_SERVICE} returned HTTP {resp.status_code}", failed=True)
    server = _servers(resp, username).get("")
    if server is None:
        return _Poll("no server listed", absent=True)
    if server.get("ready", False):
        return _Poll(
            "session is running", ready=True, started=server.get("started", "")
        )
    pending = server.get("pending")
    if pending == "stop":
        raise UserError(
            "Error: the session is stopping, not starting. "
            "Wait for get_session_status to report no active "
            "session, then call start_af_session."
        )
    return _Poll(
        f"session pending ({pending})" if pending else "session listed but not ready"
    )


def _start_call(opts: dict) -> str:
    """The start_af_session call that reuses spawn options ``opts``."""
    rest = {key: value for key, value in opts.items() if key != "profile"}
    args = [f"profile_name={opts['profile']!r}"] if "profile" in opts else []
    if rest:
        args.append(f"user_options={rest!r}")
    return f"start_af_session({', '.join(args)})" if args else "start_af_session"


# ── profile / option selection helpers ────────────────────────────────────────

# Returned when a choice cannot be elicited, so the agent can ask in chat.
_ELICIT_FALLBACK = (
    "Couldn't collect the choices interactively. Ask the user which profile and "
    "options they want — call list_af_profiles for the exact slugs, option keys, "
    "and live GPU availability — then call start_af_session again with "
    "profile_name and user_options. To skip the questions entirely, call "
    "start_af_session(use_defaults=True) to launch the default profile."
)


def _default_profile(available: list[dict]) -> Optional[dict]:
    """Return the profile marked default, else the first, else None."""
    for p in available:
        if p.get("default"):
            return p
    return available[0] if available else None


def _named_profile(available: list[dict], name: str, otherwise: str) -> dict:
    """The profile called ``name``. ``otherwise`` is the caller's way forward
    while the profile list cannot be read."""
    selected = profiles.find_profile(available, name)
    if selected is not None:
        return selected
    if not available:
        raise UpstreamError(
            f"Error: cannot check profile '{name}' — the profile list could not "
            f"be read ({profiles.profiles_error() or 'unknown reason'}). Try again "
            f"in a minute, or {otherwise}."
        )
    known = ", ".join(f'"{p["slug"]}"' for p in available)
    raise UserError(
        f"Unknown profile '{name}'. "
        f"Call list_af_profiles to see available options. "
        f"Known slugs: {known}"
    )


async def _choose_profile(
    ctx: Context,
    available: list[dict],
    profile_name: Optional[str],
    use_defaults: bool,
) -> Optional[dict]:
    """The profile to launch; None leaves the choice to the Hub (no profile list)."""
    if profile_name:
        return _named_profile(
            available,
            profile_name,
            "call start_af_session(use_defaults=True) to launch the Hub's default "
            "profile",
        )
    if use_defaults:
        return _default_profile(available)
    if len(available) <= 1:
        return available[0] if available else None
    model = single_choice_model(
        "ProfileChoice",
        [p["slug"] for p in available],
        labels=[
            p["display_name"] + (" (default)" if p.get("default") else "")
            for p in available
        ],
        default=(_default_profile(available) or {}).get("slug"),
        description="Analysis Facility session profile.",
        field="profile",
    )
    choice = await ask(ctx, "Choose a session profile.", model, _ELICIT_FALLBACK)
    return profiles.find_profile(available, choice.profile)


async def _ask_options(ctx: Context, profile: dict, opts: dict) -> None:
    """Ask for each option of ``profile`` the caller didn't specify, into ``opts``."""
    for opt_key, opt_info in profile.get("options", {}).items():
        if opt_key in opts:
            continue
        choices = profiles.labelled_choices(opt_info)
        if not choices:
            continue
        # The same availability the Hub form shows, with exhausted flavors hidden.
        gpu_map = opt_info.get("gpu")
        if gpu_map:
            labels_map, keys = apply_availability(choices, gpu_map, await free_gpus())
        else:
            labels_map, keys = choices, list(choices)
        model = single_choice_model(
            f"Option_{len(opts)}",
            keys,
            labels=[labels_map[k] for k in keys],
            default=opt_info.get("default"),
            description=opt_info.get("display_name", opt_key),
        )
        choice = await ask(
            ctx,
            f"{opt_info.get('display_name', opt_key)}:",
            model,
            _ELICIT_FALLBACK,
        )
        opts[opt_key] = choice.value


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def get_session_status() -> str:
        """Return the current status of the user's Analysis Facility session (pod).

        Includes: whether it is running, which profile and resources were selected,
        how long it has been active, and the URL to access it.
        """
        user = require_user()
        token = user["token"]
        username = user["username"]

        servers = await _user_servers(token, username)

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

        # Option keys are numbered per profile: match "interface" or "*-interface".
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

        # Both links always; the one selected at spawn time (default JupyterLab) is marked.
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
                          list_af_profiles, e.g.
                          {"3-interface": "2", "0-cpu": "3", "2-memory": "2"}.
                          Any option not supplied is elicited.
            use_defaults: Skip elicitation and start the default profile/options.
        """
        user = require_user()
        token = user["token"]
        username = user["username"]

        opts: dict = dict(user_options or {})

        available = await profiles.get_profiles()
        selected = await _choose_profile(ctx, available, profile_name, use_defaults)

        if selected is not None:
            # KubeSpawner selects the profile by slug; always include it.
            opts["profile"] = selected["slug"]
            if not use_defaults:
                await _ask_options(ctx, selected, opts)

        resp = await _spawn(token, username, opts)

        if resp.status_code == 400:
            refusal = _spawn_refusal(resp)
            if refusal == "running":
                return (
                    "Session is already running. Use get_session_status to see its URL."
                )
            if refusal == "stopping":
                raise UserError(_STILL_STOPPING)
            raise UserError(
                "Error: JupyterHub rejected the spawn request — "
                f"{response_detail(resp) or 'no reason given'}. Check the profile "
                "and options against list_af_profiles and call start_af_session "
                "again."
            )

        note = ""
        if selected is None and not available:
            note = (
                "\nNote: the profile list could not be read "
                f"({profiles.profiles_error() or 'unknown reason'}), so the Hub's default "
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
        Stored files are preserved.
        """
        user = require_user()
        token = user["token"]
        username = user["username"]

        if await _stop(token, username) == 400:
            return "No session is currently running."

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

        # This call holds the request open for the whole wait.
        timeout_seconds = max(1, min(timeout_seconds, 600))
        deadline = time.monotonic() + timeout_seconds
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
                # So a timeout can say the hub was unreachable, not "not ready".
                failed_polls += 1
                last_seen = f"{_SERVICE} unreachable — {describe_exception(exc)}"
            else:
                poll = _classify_poll(resp, username)
                last_seen = poll.seen
                if poll.ready:
                    lines = [
                        "Session is running.",
                        f"(became ready after {attempts} poll(s))",
                    ]
                    if poll.started:
                        lines.insert(1, f"started: {poll.started}")
                    lines += [
                        "",
                        "Next: get_session_status returns browser links.",
                    ]
                    return "\n".join(lines)
                if poll.failed:
                    failed_polls += 1
                elif poll.absent:
                    # A spawn in progress is always listed; two looks rule out a gap.
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
                else:
                    absent_polls = 0

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_POLL_INTERVAL, remaining))

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

        # Reported: "same options as before" must not silently become the defaults.
        prior_opts: dict = {}
        prior_note = ""
        try:
            servers = await _user_servers(token, username)
        except UpstreamError as exc:
            prior_note = f" (the previous options could not be read: {exc})"
        else:
            prior_opts = servers.get("", {}).get("user_options", {}) or {}

        spawn_opts: dict = dict(user_options or prior_opts)
        if profile_name:
            spawn_opts["profile"] = _named_profile(
                await profiles.get_profiles(),
                profile_name,
                "call restart_af_session without profile_name to keep the current "
                "profile",
            )["slug"]

        stopped = await _stop(token, username)
        start_call = _start_call(spawn_opts)
        try:
            if stopped == 202:
                await _await_stop(token, username)
            elif stopped != 400:
                await asyncio.sleep(3)  # let Kubernetes terminate the pod
            start = await _spawn(token, username, spawn_opts)
        except Failure as exc:
            raise type(exc)(
                f"Session was stopped, but the restart failed. {exc} "
                f"Use {start_call} to try again."
            )

        if start.status_code == 400:
            if _spawn_refusal(start) != "rejected":
                raise UpstreamError(
                    "Session stopped but the pod is still terminating. Wait for "
                    "get_session_status to report no active session, then call "
                    f"{start_call} to complete the restart."
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
