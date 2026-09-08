#!/usr/bin/env python3
"""Maintain the Purdue AF ``[otel]`` block in a user-owned ``config.toml``.

    otel-toml-block.py <block.toml|-> <target.toml>
    otel-toml-block.py --remove <target.toml>

The block always goes last (a table header owns every key after it), any
existing ``[otel]`` table is removed first (``codex mcp add`` rewrites this
file and may drop the markers; a duplicate table would not parse), and the
result is parsed before it is written.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

BEGIN = "# >>> BEGIN PURDUE AF — managed, edits inside are overwritten >>>"
END = "# <<< END PURDUE AF <<<"

# A TOML table header: [otel], [otel.exporter], [[otel.x]] — but not [otelemetry].
_OTEL_TABLE = re.compile(r"^\s*\[\[?\s*otel\s*(?:\.|\])")
_ANY_TABLE = re.compile(r"^\s*\[")


def strip_managed(lines: list[str]) -> list[str]:
    """Drop a previous marked block, wherever it sits in the file."""
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == BEGIN)
        end = next(i for i, line in enumerate(lines) if line.strip() == END)
    except StopIteration:
        return lines
    if end < start:
        return lines
    return lines[:start] + lines[end + 1 :]


def strip_otel_tables(lines: list[str]) -> list[str]:
    """Drop every ``[otel…]`` table, including one codex rewrote unmarked."""
    kept: list[str] = []
    dropping = False
    for line in lines:
        if _OTEL_TABLE.match(line):
            dropping = True
            continue
        if dropping and _ANY_TABLE.match(line):
            dropping = False
        if not dropping:
            kept.append(line)
    return kept


def apply_block(existing: str, block: str) -> str:
    lines = strip_otel_tables(strip_managed(existing.splitlines()))
    head = "\n".join(lines).rstrip("\n")
    body = "\n".join([BEGIN, block.strip(), END])
    return (f"{head}\n\n{body}\n" if head else f"{body}\n").lstrip("\n")


def _write_if_valid(target: Path, updated: str) -> int:
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        print(
            f"otel-toml-block: refusing to write unparseable {target}: {exc}",
            file=sys.stderr,
        )
        return 1
    if target.is_file() and target.read_text() == updated:
        print(f"otel-toml-block: {target} already current")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(updated)
    print(f"otel-toml-block: updated {target}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--remove":
        target = Path(argv[2])
        if not target.is_file():
            return 0
        existing = target.read_text()
        updated = "\n".join(
            strip_otel_tables(strip_managed(existing.splitlines()))
        ).strip()
        if not updated:
            target.unlink()
            print(f"otel-toml-block: removed {target}, it held only the block")
            return 0
        return _write_if_valid(target, updated + "\n")

    if len(argv) != 3:
        raise SystemExit(__doc__)

    block, target = argv[1], Path(argv[2])
    content = sys.stdin.read() if block == "-" else Path(block).read_text()
    existing = target.read_text() if target.is_file() else ""
    # A config the user (or codex) already broke is not ours to rewrite:
    # appending to it would only bury the real error.
    if existing.strip():
        try:
            tomllib.loads(existing)
        except tomllib.TOMLDecodeError as exc:
            print(
                f"otel-toml-block: {target} is not valid TOML ({exc}); left alone",
                file=sys.stderr,
            )
            return 1
    return _write_if_valid(target, apply_block(existing, content))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
