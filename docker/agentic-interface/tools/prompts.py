"""MCP prompts — invocable, client-portable playbooks for the AF workflows.

These describe the *sequence of tool calls* only; next-step hints returned by
the tools remain the primary, always-read steering channel.

Any MCP client can surface these; in Claude Code they appear as
``/mcp__purdue-af-agentic-interface__<name>``.
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
            "5. Report the URL from get_session_status."
        )

    @mcp.prompt()
    def restart_session() -> str:
        """Restart the AF session, preserving or changing profile/options."""
        return (
            "Restart the user's AF session:\n"
            "1. restart_af_session — pass profile_name/user_options to change "
            "configuration, or omit them to keep the current setup.\n"
            "2. wait_for_session.\n"
            "3. Report the URL from get_session_status."
        )

    @mcp.prompt()
    def create_cluster() -> str:
        """Create a Dask Gateway cluster, asking the user for backend + env."""
        return (
            "Create a Dask Gateway cluster for the user. Ask these as "
            "multiple-choice questions (use the client's choice UI if available) "
            "before calling create_dask_cluster:\n"
            "1. Backend — 'k8s' (Geddes Kubernetes) or 'slurm' (Hammer)?\n"
            "2. Worker environment —\n"
            "   • 'global': shared pixi env at /work/pixi/global (k8s only), or\n"
            "   • 'pixi': the user's own pixi project (ask for pixi_project path "
            "and optional pixi_env), or\n"
            "   • 'conda': the user's own conda env (ask for conda_env path).\n"
            "3. Worker size — 'default' (1 core / 4 GiB) or 'custom' (then ask "
            "for worker_cores and worker_memory in GiB).\n"
            "4. Worker count to start with — 0, 10, 50, or a custom number "
            "(n_workers).\n"
            "Then call create_dask_cluster with the chosen arguments. "
            "create_dask_cluster will also elicit these directly if you call it "
            "without them. Notes: Slurm workers cannot see /work, so 'global' is "
            "k8s-only and Slurm envs must live on /depot. Call "
            "list_dask_cluster_options first if you want exact limits/defaults."
        )

    @mcp.prompt()
    def stop_session() -> str:
        """Stop the AF session (storage is preserved)."""
        return (
            "Stop the user's AF session:\n"
            "1. stop_af_session — storage (home, /work) is preserved."
        )
