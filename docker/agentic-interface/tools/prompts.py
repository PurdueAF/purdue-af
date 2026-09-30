"""MCP prompts — invocable, client-portable playbooks.

Tool results already name the next step, so only a workflow that needs
input the tools cannot ask for themselves earns a prompt: creating a Dask
cluster, whose questions a client without elicitation must ask in chat.
In Claude Code it appears as ``/mcp__purdue-af-agentic-interface__create_cluster``.
"""

from typing import Any


def register(mcp: Any) -> None:
    @mcp.prompt()
    def create_cluster() -> str:
        """Create a Dask Gateway cluster, asking the user for env, size and count."""
        return (
            "Create a Dask Gateway cluster for the user. Ask these as "
            "multiple-choice questions (use the client's choice UI if available) "
            "before calling create_dask_cluster:\n"
            "1. Worker environment —\n"
            "   • 'global': shared pixi env at /work/pixi/global, or\n"
            "   • 'pixi': the user's own pixi project (ask for pixi_project path "
            "and optional pixi_env), or\n"
            "   • 'conda': the user's own conda env (ask for conda_env path).\n"
            "2. Worker size — 'default' (1 core / 4 GiB) or 'custom' (then ask "
            "for worker_cores and worker_memory in GiB).\n"
            "3. Worker count to start with — 0, 10, 50, or a custom number "
            "(n_workers).\n"
            "Then call create_dask_cluster with the chosen arguments. "
            "create_dask_cluster will also elicit these directly if you call it "
            "without them. Call list_dask_cluster_options first if you want "
            "exact limits/defaults."
        )
