"""What each tool does to the facility, as MCP tool annotations: clients such
as PurdueAF Agent decide from these which tools a caller may be offered."""

from mcp.types import ToolAnnotations

# Reads only.
READS = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=False
)
# Starts, creates or scales something of the caller's.
CHANGES = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, open_world_hint=False
)
# Stops or deletes something of the caller's, losing what runs in it.
DESTROYS = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, open_world_hint=False
)
