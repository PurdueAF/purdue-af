"""MCP prompts — invocable, client-portable playbooks.

Tool results already name the next step, so only a workflow that needs
input the tools cannot ask for themselves earns a prompt: creating a Dask
cluster, whose questions a client without elicitation must ask in chat.
"""

from mcp.server.mcpserver import MCPServer

from tools.dask import CREATE_MENU


def register(mcp: MCPServer) -> None:
    @mcp.prompt()
    def create_cluster() -> str:
        """Create a Dask Gateway cluster, asking the user for env, size and count."""
        return (
            "Create a Dask Gateway cluster for the user. Ask these as "
            "multiple-choice questions (use the client's choice UI if available) "
            "before calling create_dask_cluster with the chosen arguments:\n"
            f"{CREATE_MENU}\n"
            "Call list_dask_cluster_options first if you want exact limits/defaults."
        )
