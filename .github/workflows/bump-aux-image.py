#!/usr/bin/env python3
"""Bump a VERSIONED auxiliary image (e.g. af-node-monitor) across all
manifests under apps/ — the file-editing half of release-image.yml.

Aux images come in two channels:
  - continuous (`:latest` refs, e.g. agentic-interface, af-pod-monitor):
    the ci.yml publish stage moves the ghcr `latest` tag after every fully
    green main pipeline; manifests never change and this script REFUSES to
    touch them;
  - versioned (semver refs, e.g. af-node-monitor): promoted manually via
    release-image.yml, which retags the tested exact-state digest and
    calls this script to rewrite every manifest reference.

The manifests are the single source of truth for the current version;
rewrites also normalize the registry to the geddes ghcr-proxy-cache path.

Usage:
  bump-aux-image.py --name af-node-monitor --print-current
  bump-aux-image.py --name af-node-monitor --bump patch|minor|major [--dry-run]
  bump-aux-image.py --name af-node-monitor --set 0.2.0 [--dry-run]
"""

import argparse
import re
import sys
from pathlib import Path

APPS_DIR = Path("apps")
PROXY = "geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf"
VERSION_RE = r"\d+\.\d+\.\d+"


def bump_version(cur, kind):
    major, minor, patch = map(int, cur.split("."))
    return {
        "major": f"{major + 1}.0.0",
        "minor": f"{major}.{minor + 1}.0",
        "patch": f"{major}.{minor}.{patch + 1}",
    }[kind]


def find_refs(name, apps_dir=APPS_DIR):
    """→ ({version or 'latest': count}, [(path, text)]) for `name` image refs."""
    pattern = re.compile(rf"image: \S*/{re.escape(name)}:(\S+)")
    versions, files = {}, []
    for path in sorted(apps_dir.rglob("*.yaml")):
        text = path.read_text()
        tags = pattern.findall(text)
        if tags:
            files.append((path, text))
            for t in tags:
                versions[t] = versions.get(t, 0) + 1
    return versions, files


def resolve_current(versions, name):
    semvers = sorted(v for v in versions if re.fullmatch(VERSION_RE, v))
    if not semvers:
        if "latest" in versions:
            sys.exit(
                f"{name} is on the continuous ':latest' channel — CI moves its "
                "tag automatically; there is no version to bump"
            )
        sys.exit(f"no {name} image refs found under apps/")
    if len(semvers) > 1:
        sys.exit(f"{name} is pinned inconsistently across manifests: {semvers}")
    return semvers[0]


def apply(name, new_version, apps_dir=APPS_DIR, dry_run=False):
    """Rewrite every `<anything>/{name}:<semver>` ref → proxy path + new
    version. → number of replacements."""
    pattern = re.compile(rf"image: \S*/{re.escape(name)}:{VERSION_RE}\b")
    replacement = f"image: {PROXY}/{name}:{new_version}"
    total = 0
    for path in sorted(apps_dir.rglob("*.yaml")):
        text = path.read_text()
        updated, n = pattern.subn(replacement, text)
        if n:
            total += n
            if not dry_run:
                path.write_text(updated)
            print(f"  {path}: {n} ref(s)", file=sys.stderr)
    if total == 0:
        sys.exit(f"nothing rewritten for {name} — no semver-pinned refs found")
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--name", required=True, help="image name, e.g. af-node-monitor"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--print-current", action="store_true")
    group.add_argument("--bump", choices=["patch", "minor", "major"])
    group.add_argument("--set", dest="explicit", metavar="X.Y.Z")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    versions, _ = find_refs(args.name)
    cur = resolve_current(versions, args.name)
    if args.print_current:
        print(cur)
        return

    if args.explicit:
        if not re.fullmatch(VERSION_RE, args.explicit):
            sys.exit(f"--set expects X.Y.Z, got {args.explicit!r}")
        new = args.explicit
    else:
        new = bump_version(cur, args.bump)

    apply(args.name, new, dry_run=args.dry_run)
    print(
        f"{args.name}: {cur} -> {new}{' — dry run' if args.dry_run else ''}",
        file=sys.stderr,
    )
    print(new)


if __name__ == "__main__":
    main()
