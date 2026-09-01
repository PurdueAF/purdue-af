#!/usr/bin/env python3
"""Rewrite the agentic-interface image version in its Deployment — the
file-editing half of the image release workflow
(.github/workflows/release-image.yml, image=agentic-interface).

deployment.yaml is the single source of truth for "what production runs":
the current version is READ from it (--print-current), and a release
rewrites the image tag with a count-verified substitution — if the file
drifts (the image line moves or is renamed), the script fails loudly
instead of producing a half-bumped release.

Before the first release the manifest carries the `latest` channel tag;
--print-current then reports 0.0.0, so `--bump patch` mints 0.0.1 (pass
--set X.Y.Z to pick a different starting version).

Usage:
  bump-agentic-version.py --print-current
  bump-agentic-version.py --bump patch|minor|major   [--dry-run]
  bump-agentic-version.py --set 0.2.0                [--dry-run]
"""

import argparse
import re
import sys
from pathlib import Path

DEFAULT_FILE = Path("apps/agentic-interface/deployment.yaml")
VERSION_RE = r"\d+\.\d+\.\d+"
# The image ref as it appears in the Deployment (geddes Harbor proxy of ghcr;
# the CI-promoted semver tags live on ghcr.io/purdueaf/agentic-interface).
IMAGE_LINE_RE = rf"(?m)^(\s*image: \S+/agentic-interface:)(latest|{VERSION_RE})$"


def bump_version(cur: str, kind: str) -> str:
    major, minor, patch = map(int, cur.split("."))
    return {
        "major": f"{major + 1}.0.0",
        "minor": f"{major}.{minor + 1}.0",
        "patch": f"{major}.{minor}.{patch + 1}",
    }[kind]


def current_version(text: str) -> str:
    m = re.search(IMAGE_LINE_RE, text)
    if not m:
        sys.exit(
            "cannot find the agentic-interface image line in deployment.yaml "
            "— layout changed?"
        )
    tag = m.group(2)
    if tag == "latest":
        # Pre-first-release state: bump from a zero baseline.
        print("current tag is 'latest' — treating as 0.0.0", file=sys.stderr)
        return "0.0.0"
    return tag


def apply(text: str, new_version: str) -> str:
    """→ new text; the substitution must match exactly once."""
    text, n = re.subn(IMAGE_LINE_RE, rf"\g<1>{new_version}", text)
    if n != 1:
        sys.exit(
            f"expected exactly 1 agentic-interface image line, found {n} — "
            "deployment.yaml layout changed; update bump-agentic-version.py"
        )
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--print-current", action="store_true")
    group.add_argument("--bump", choices=["patch", "minor", "major"])
    group.add_argument("--set", dest="explicit", metavar="X.Y.Z")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    text = args.file.read_text()
    cur = current_version(text)
    if args.print_current:
        print(cur)
        return

    if args.explicit:
        if not re.fullmatch(VERSION_RE, args.explicit):
            sys.exit(f"--set expects X.Y.Z, got {args.explicit!r}")
        new = args.explicit
    else:
        new = bump_version(cur, args.bump)

    updated = apply(text, new)
    if not args.dry_run:
        args.file.write_text(updated)
    # stdout carries ONLY the new version (workflow captures it); log to stderr
    print(
        f"{cur} -> {new} ({args.file}{' — dry run' if args.dry_run else ''})",
        file=sys.stderr,
    )
    print(new)


if __name__ == "__main__":
    main()
