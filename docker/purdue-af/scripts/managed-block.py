#!/usr/bin/env python3
"""Maintain a Purdue AF managed section inside a user-owned agent file.

Agent instruction files (`~/.codex/AGENTS.md`, `~/.claude/CLAUDE.md`) belong to
the user, but the AF part of them has to track the platform as the agentic
interface evolves. So the AF content lives between markers: everything inside
is replaced on every session start, everything outside is never touched.

    managed-block.py <section.md> <target.md>
    managed-block.py --remove <target.md>

Idempotent — running it twice produces the same file. If the markers are
absent the block is appended, so an existing hand-written file keeps its
content and simply gains the AF section at the end.

`--remove` is the reverse, for retiring a target: it takes the block back out
and deletes the file if the block was all it held. A file with no markers is
never touched — it was the user's to begin with.
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


def strip_block(existing: str) -> str | None:
    """`existing` without the managed block, or None if it has no block.

    None distinguishes "nothing of ours is in this file" — leave it alone —
    from "the block was all it held", which comes back as an empty string.
    """
    start = existing.find(BEGIN)
    end = existing.find(END)
    if start == -1 or end == -1 or end < start:
        return None
    head = existing[:start].rstrip("\n")
    tail = existing[end + len(END) :].strip("\n")
    parts = [p for p in (head, tail) if p]
    return "\n\n".join(parts) + "\n" if parts else ""


def remove(target_path: Path) -> int:
    """Take the managed block back out of a target we no longer write."""
    if not target_path.is_file():
        print(f"managed-block: no {target_path}, nothing to remove")
        return 0
    remainder = strip_block(target_path.read_text())
    if remainder is None:
        print(f"managed-block: {target_path} holds no managed block, left alone")
    elif remainder.strip():
        target_path.write_text(remainder)
        print(f"managed-block: removed the managed block from {target_path}")
    else:
        target_path.unlink()
        print(f"managed-block: removed {target_path}, it held only the block")
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--remove":
        return remove(Path(sys.argv[2]))
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
