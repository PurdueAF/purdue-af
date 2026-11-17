#!/usr/bin/env python3
"""Maintain a Purdue AF managed section inside a user-owned agent file.

The AF content lives between markers: everything inside is replaced on every
run, everything outside is never touched.

    managed-block.py <section.md> <target.md>
    managed-block.py --remove <target.md>

Idempotent — running it twice produces the same file. If the markers are
absent the block is appended, so an existing hand-written file keeps its
content and simply gains the AF section at the end. `--remove` takes the
section out again, and the file with it when nothing else is in it.
"""

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
    end = existing.find(END, start)

    if start == -1 or end == -1 or end < start:
        if not existing.strip():
            return block + "\n"
        # keep exactly one blank line between the user's content and ours
        return existing.rstrip("\n") + "\n\n" + block + "\n"

    head = existing[:start].rstrip("\n")
    tail = existing[end + len(END) :].lstrip("\n")
    parts = [p for p in (head, block, tail.rstrip("\n")) if p]
    return "\n\n".join(parts) + "\n"


def remove_block(existing: str) -> str:
    """`existing` without the managed block; unchanged when it has none."""
    start = existing.find(BEGIN)
    end = existing.find(END, start)
    if start == -1 or end == -1:
        return existing
    head = existing[:start].rstrip("\n")
    tail = existing[end + len(END) :].strip("\n")
    parts = [p for p in (head, tail) if p]
    return "\n\n".join(parts) + "\n" if parts else ""


def remove(target_path: Path) -> int:
    if not target_path.is_file():
        return 0
    existing = target_path.read_text()
    updated = remove_block(existing)
    if updated == existing:
        return 0
    if updated:
        target_path.write_text(updated)
    else:
        target_path.unlink()
    print(f"managed-block: removed the section from {target_path}")
    return 0


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    if sys.argv[1] == "--remove":
        return remove(Path(sys.argv[2]))
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
