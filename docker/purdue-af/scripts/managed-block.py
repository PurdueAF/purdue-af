#!/usr/bin/env python3
"""Maintain a Purdue AF managed section inside a user-owned agent file.

Agent instruction files (`~/.codex/AGENTS.md`, `~/.claude/CLAUDE.md`) belong to
the user, but the AF part of them has to track the platform as the agentic
interface evolves. So the AF content lives between markers: everything inside
is replaced on every session start, everything outside is never touched.

    managed-block.py <section.md> <target.md>

Idempotent — running it twice produces the same file. If the markers are
absent the block is appended, so an existing hand-written file keeps its
content and simply gains the AF section at the end.
"""

from __future__ import annotations

import sys
from pathlib import Path

BEGIN = "<!-- BEGIN PURDUE AF — managed, edits inside are overwritten -->"
END = "<!-- END PURDUE AF -->"


def render(section: str) -> str:
    return f"{BEGIN}\n{section.strip()}\n{END}"


def apply_block(existing: str, section: str) -> str:
    """Replace the managed block in `existing`, or append it."""
    block = render(section)
    start = existing.find(BEGIN)
    end = existing.find(END)

    if start == -1 or end == -1 or end < start:
        if not existing.strip():
            return block + "\n"
        # keep exactly one blank line between the user's content and ours
        return existing.rstrip("\n") + "\n\n" + block + "\n"

    head = existing[:start].rstrip("\n")
    tail = existing[end + len(END) :].lstrip("\n")
    parts = [p for p in (head, block, tail.rstrip("\n")) if p]
    return "\n\n".join(parts) + "\n"


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    section_path, target_path = Path(sys.argv[1]), Path(sys.argv[2])
    section = section_path.read_text()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    existing = target_path.read_text() if target_path.is_file() else ""
    updated = apply_block(existing, section)
    if updated != existing:
        target_path.write_text(updated)
        print(f"managed-block: updated {target_path}")
    else:
        print(f"managed-block: {target_path} already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
