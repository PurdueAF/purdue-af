"""MCP prompts — invocable, client-portable playbooks for the AF workflows.

These describe the *sequence of tool calls* only; the heavy artifacts (e.g. the
SSH bootstrap command) are produced by the tools themselves at call time, so
there is a single source of truth and nothing is duplicated here.

Any MCP client can surface these; in Claude Code they appear as
``/mcp__purdue-af-agentic-interface__<name>``.  They complement — and never
contradict — the next-step hints returned by the tools, which remain the
primary, always-read steering channel.
"""


def register(mcp) -> None:
    @mcp.prompt()
    def launch_session() -> str:
        """Start the user's Purdue Analysis Facility session and wait until ready."""
        return (
            "Start the user's Purdue Analysis Facility session:\n"
            "1. get_session_status — if already running, report the URL and stop.\n"
            "2. (optional) list_af_profiles if the user wants a non-default "
            "profile or resources.\n"
            "3. start_af_session with any chosen profile/options.\n"
            "4. wait_for_session — blocks until the pod is ready.\n"
            "5. Report the URL from get_session_status, then ask whether they "
            "want to connect via SSH."
        )

    @mcp.prompt()
    def connect_session() -> str:
        """Connect to the user's running AF session over SSH."""
        return (
            "Connect to the user's running AF session over SSH "
            "(only when the user has asked to):\n"
            "1. get_session_status — confirm status is 'running'.\n"
            "2. prepare_ssh_connection — run the exact command it returns "
            "locally (a single Bash call).\n"
            "3. If the output is 'ALREADY_CONNECTED', you're done. Otherwise pass "
            "the printed public key to connect_to_session(public_key=...).\n"
            '4. Verify: ssh PurdueAF "hostname".\n'
            'Thereafter run pod commands with ssh PurdueAF "<cmd>".'
        )

    @mcp.prompt()
    def restart_session() -> str:
        """Restart the AF session, preserving or changing profile/options."""
        return (
            "Restart the user's AF session:\n"
            "1. Close any open SSH master: ssh -O exit PurdueAF 2>/dev/null; true\n"
            "2. restart_af_session — pass profile_name/user_options to change "
            "configuration, or omit them to keep the current setup.\n"
            "3. wait_for_session.\n"
            "4. Ask whether they want to reconnect; if so, follow connect_session."
        )

    @mcp.prompt()
    def stop_session() -> str:
        """Stop the AF session (storage is preserved)."""
        return (
            "Stop the user's AF session:\n"
            "1. Close any open SSH master: ssh -O exit PurdueAF 2>/dev/null; true\n"
            "2. stop_af_session — storage (home, /work) is preserved."
        )

    @mcp.prompt()
    def recover_ssh() -> str:
        """Fix a broken SSH connection after a pod restart or network drop."""
        return (
            "If SSH commands fail after a pod restart or network drop, clear the "
            "stale ControlMaster socket and reconnect:\n"
            'rm -f ~/.ssh/control-af-* && ssh PurdueAF "hostname"'
        )
