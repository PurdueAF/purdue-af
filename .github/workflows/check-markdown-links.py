#!/usr/bin/env python3
"""Every relative link in the given Markdown files must resolve to a file or
directory in the repository. Runs as a pre-commit hook; exits 1 on the first
broken link, naming each."""

import re
import sys
from pathlib import Path

FENCE = re.compile(r"^(```|~~~).*?^\1\s*$", re.M | re.S)
INLINE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^\s<>()]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+)>?", re.M)
SKIP = ("http://", "https://", "mailto:", "#", "/", "tel:", "ftp://")


def targets(text: str) -> list[tuple[int, str]]:
    """(line number, target) for every link that points inside the tree."""
    found = []
    for pattern in (INLINE, REFERENCE):
        for m in pattern.finditer(text):
            target = m.group(1)
            if target.startswith(SKIP) or "://" in target:
                continue
            line = text.count("\n", 0, m.start()) + 1
            found.append((line, target))
    return sorted(found)


def broken(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    # keep line numbers: blank out fenced blocks instead of removing them
    text = FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    out = []
    for line, target in targets(text):
        clean = target.split("#", 1)[0].split("?", 1)[0]
        if not clean:
            continue
        if not (path.parent / clean).exists():
            out.append(f"{path}:{line}: broken link -> {target}")
    return out


def main(argv: list[str]) -> int:
    failures = [f for arg in argv for f in broken(Path(arg))]
    for failure in failures:
        print(failure)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
