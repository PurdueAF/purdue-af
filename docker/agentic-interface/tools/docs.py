"""The user documentation, read on each use: kubelet updates the mounted pages in place."""

import re
from pathlib import Path

from annotations import FACILITY, READS
from config import DOCS_DIR, DOCS_URL
from errors import UpstreamError, UserError
from mcp.server.mcpserver import MCPServer

_TITLE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _pages() -> dict[str, Path]:
    return {path.stem: path for path in sorted(Path(DOCS_DIR).glob("*.md"))}


def index() -> str:
    """One line per page: its name and its title."""
    lines = []
    for name, path in _pages().items():
        title = _TITLE.search(path.read_text())
        lines.append(f"- {name}: {title.group(1) if title else name}")
    return "\n".join(lines)


def _url(page: str) -> str:
    return f"{DOCS_URL}/" if page == "index" else f"{DOCS_URL}/{page}/"


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READS, meta=FACILITY)
    async def read_af_doc(page: str) -> str:
        """Read one page of the Purdue AF user documentation, as Markdown.

        The how-to guides and reference users read: storage, software
        environments, data access, Dask, Ray, GPUs, Slurm, SSH and
        troubleshooting. Read the page before telling someone how to do
        something on the facility. This server's instructions list the pages.

        Args:
            page: A page name, like 'guide-dask-gateway'. A link between
                  pages names one: 'storage.md#quotas' is the page 'storage'.
        """
        pages = _pages()
        if not pages:
            raise UpstreamError(
                "Error: the documentation is not available to this service "
                f"just now. It is published at {DOCS_URL}/."
            )
        name = page.split("#")[0].strip().strip("/").removesuffix(".md")
        path = pages.get(name)
        if path is None:
            raise UserError(f"Error: there is no page {page!r}. The pages:\n{index()}")
        return f"{_url(name)}\n\n{path.read_text()}"
