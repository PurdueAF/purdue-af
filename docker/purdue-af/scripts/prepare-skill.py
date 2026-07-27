#!/usr/bin/env python3
"""Adapt the repo's agentic-interface skill for shipping inside the AF image.

The committed skill (`.claude/skills/`) targets a laptop: it opens with a
one-time setup blockquote telling the reader to mint a JupyterHub token and
register the MCP server by hand. Inside a session both are already done —
`config-agents.sh` registers the server and the token comes from the pod's
environment — so that block is not just noise, it is wrong.

Everything else is identical, so the skill stays a single source of truth and
this script rewrites only the preamble at build time.

    prepare-skill.py <source SKILL.md> <destination SKILL.md>
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "One-time setup"
IN_SESSION_NOTE = """> **Already set up** — this skill drives the
> `purdue-af-agentic-interface` MCP server, which every Purdue AF session
> registers at startup using the session's own credentials. There is no token
> to create and nothing to configure. If the tools are missing, the MCP
> service is down rather than unconfigured.
>
> Username and active pod are resolved automatically."""


def strip_setup_block(text: str) -> str:
    """Replace the leading setup blockquote with the in-session note.

    Raises if the block is gone: silently shipping laptop setup instructions
    to every session is worse than a failed build.
    """
    lines = text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if line.startswith(">") and MARKER in line:
            start = i
            break
    if start is None:
        raise SystemExit(
            f"prepare-skill: no blockquote containing {MARKER!r} — the skill's "
            "preamble changed; update this script instead of shipping it as is"
        )
    end = start
    while end + 1 < len(lines) and lines[end + 1].startswith(">"):
        end += 1
    return "\n".join(lines[:start] + IN_SESSION_NOTE.splitlines() + lines[end + 1 :])


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    source, destination = Path(sys.argv[1]), Path(sys.argv[2])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(strip_setup_block(source.read_text()) + "\n")
    print(f"prepare-skill: wrote {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
