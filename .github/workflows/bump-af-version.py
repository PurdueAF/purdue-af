#!/usr/bin/env python3
"""Rewrite the production AF version across values.yaml — the file-editing
half of the Release AF workflow (.github/workflows/release-af.yml).

values.yaml is the single source of truth for "what production runs": the
current version is READ from it (--print-current), and a release rewrites
every place it appears with count-verified substitutions — if the file
drifts (a spot is added/removed/renamed), the script fails loudly instead
of producing a half-bumped release.

Spots rewritten (each must match exactly once):
  1. singleuser.image.name        → the production registry/repo
  2. singleuser.image.tag         → the new version
  3. extraLabels.docker_image_tag → the new version (feeds dashboards)
  4. the production profile display_name ("Purdue AF <version> – ...")
  5. the production profile kubespawner image ref

The purdue-af IMAGE is versioned with its own semver (0.12.x), on a
separate cadence from the platform's CalVer tags (2026.M.SEQ) — see
release-af-image.yml vs release-af.yml.

Usage:
  bump-af-version.py --print-current
  bump-af-version.py --bump patch|minor|major   [--dry-run]
  bump-af-version.py --set 0.13.0               [--dry-run]
"""

import argparse
import re
import sys
from pathlib import Path

DEFAULT_FILE = Path("apps/jupyterhub/jupyterhub/values.yaml")
# Same pull path as the pre-release profile (geddes Harbor proxy of ghcr;
# the CI-promoted semver tags live on ghcr.io/purdueaf/purdue-af).
DEFAULT_REGISTRY = "geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/purdue-af"
VERSION_RE = r"\d+\.\d+\.\d+"


def bump_version(cur, kind):
    major, minor, patch = map(int, cur.split("."))
    return {
        "major": f"{major + 1}.0.0",
        "minor": f"{major}.{minor + 1}.0",
        "patch": f"{major}.{minor}.{patch + 1}",
    }[kind]


def current_version(text):
    m = re.search(rf'docker_image_tag: "({VERSION_RE})"', text)
    if not m:
        sys.exit("cannot find docker_image_tag in values.yaml — layout changed?")
    return m.group(1)


def apply(text, new_version, registry):
    """→ new text; every substitution must match exactly once."""
    subs = [
        # (what, pattern, replacement)
        (
            "singleuser image name",
            r'(?m)^(\s*name: ")[^"]*/purdue-af(")',
            rf"\g<1>{registry}\g<2>",
        ),
        (
            "singleuser image tag",
            rf'(?m)^(\s*tag: "){VERSION_RE}(")',
            rf"\g<1>{new_version}\g<2>",
        ),
        (
            "docker_image_tag pod label",
            rf'(docker_image_tag: "){VERSION_RE}(")',
            rf"\g<1>{new_version}\g<2>",
        ),
        (
            "production profile display name",
            rf'(display_name: "Purdue AF ){VERSION_RE}',
            rf"\g<1>{new_version}",
        ),
        (
            "production profile image",
            rf'(image: ")[^"]*/purdue-af:{VERSION_RE}(")',
            rf"\g<1>{registry}:{new_version}\g<2>",
        ),
    ]
    for what, pattern, repl in subs:
        text, n = re.subn(pattern, repl, text)
        if n != 1:
            sys.exit(
                f"expected exactly 1 match for {what}, found {n} — "
                "values.yaml layout changed; update bump-af-version.py"
            )
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--print-current", action="store_true")
    group.add_argument("--bump", choices=["patch", "minor", "major"])
    group.add_argument("--set", dest="explicit", metavar="X.Y.Z")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
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

    updated = apply(text, new, args.registry)
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
