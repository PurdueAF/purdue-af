#!/usr/bin/env python3
"""Per-component deployment status for the README dashboard.

Answers one question for every deployed component: **is what's on the
cluster the same as what's on `main`, and if not, why not?**

Everything is derived from git refs alone — no cluster access — because the
two Flux channels track refs in this repo:

    core    newest platform tag `YYYY.M.SEQ`  (deploy/core-production)
    experimental  branch `main-validated`           (deploy/experimental)

`main-validated` doubles as the validation boundary: the ci.yml publish
stage only advances it when every stage of that commit was green. So for a
component's own paths:

    no commits in <deployed>..main            → deployed
    commits, but none past main-validated     → validated, awaiting release
    commits past main-validated               → not validated yet (CI state
                                                decides: running vs failed)

Components and their input paths are read from the deploy kustomizations, so
a component added there appears here automatically. The purdue-af image gets
its own row: it ships on a separate semver stream (pinned in values.yaml),
and its input paths come from image-inputs.sh — the same list that
content-addresses the image in CI.

Writes shields.io endpoint JSON per component (see --out) plus a markdown
table on stdout for the workflow job summary.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml

REPO = Path(__file__).resolve().parents[2]
CHANNELS = {
    "core": Path("deploy/core-production/kustomization.yaml"),
    "experimental": Path("deploy/experimental/kustomization.yaml"),
}
IMAGE_INPUTS = Path(".github/workflows/image-inputs.sh")
VALUES_YAML = Path("apps/jupyterhub/jupyterhub/values.yaml")

COLORS = {
    "deployed": "brightgreen",
    "awaiting release": "blue",
    "validating": "yellow",
    "failed CI": "red",
    "unknown": "lightgrey",
}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def ref_exists(ref: str) -> bool:
    try:
        git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return True
    except subprocess.CalledProcessError:
        return False


def latest_platform_tag() -> str | None:
    """Newest CalVer tag — what core Flux tracks."""
    tags = [
        t
        for t in git("tag", "-l", "2*").splitlines()
        if re.fullmatch(r"\d+\.\d+\.\d+", t)
    ]
    if not tags:
        return None
    return max(tags, key=lambda t: [int(p) for p in t.split(".")])


def commits_touching(base: str, head: str, paths: Iterable[str]) -> int:
    """Number of commits in base..head that touch any of `paths`."""
    out = git("log", "--oneline", f"{base}..{head}", "--", *paths)
    return len([line for line in out.splitlines() if line.strip()])


def _read_kustomization(
    kustomization: Path,
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """→ (resource paths, [(generator name, its file paths)]), repo-relative.

    YAML-parsed, so commented-out entries are correctly ignored.
    """
    doc = yaml.safe_load((REPO / kustomization).read_text()) or {}
    base = (REPO / kustomization).parent

    def rel(item: str) -> str | None:
        resolved = (base / item.split("=", 1)[-1]).resolve()
        try:
            return str(resolved.relative_to(REPO))
        except ValueError:  # outside the repo
            return None

    resources = [r for r in (rel(x) for x in doc.get("resources") or []) if r]
    generators = []
    for generator in doc.get("configMapGenerator") or []:
        files = [f for f in (rel(x) for x in generator.get("files") or []) if f]
        if files:
            generators.append((generator.get("name", ""), files))
    return resources, generators


def _component_of(path: str) -> str:
    """apps/<group>/<name> at most — extraFiles/, dashboards/ etc. belong to
    the component above them, not beside it."""
    p = Path(path)
    parent = p.parent if p.suffix else p
    return str(Path(*parent.parts[:3]))


def _generator_owner(
    name: str, files: list[str], components: dict[str, list[str]]
) -> str:
    """Which component a ConfigMap belongs to.

    Usually its own files say so. When they all live outside apps/ (scripts
    and env manifests under docker/ or pixi/), fall back to the component
    holding a manifest named after the ConfigMap — that is the workload
    mounting it.
    """
    owners = {_component_of(f) for f in files if f.startswith("apps/")}
    if owners:
        return sorted(owners)[0]
    stem = name.removesuffix("-config").removesuffix("-cm")
    for component, paths in components.items():
        if any(stem in Path(p).stem for p in paths):
            return component
    return name


def discover_components(channel: str) -> dict[str, list[str]]:
    """{component: [input paths]} for one channel.

    Components come from `resources`; each ConfigMap generator adds its files
    to the component that owns it, so e.g. `pixi/global/pixi.lock` counts as
    an input of pixi-global-sync rather than a phantom component of its own.
    """
    resources, generators = _read_kustomization(CHANNELS[channel])
    components: dict[str, list[str]] = {}
    for path in resources:
        # HelmRepository objects are source pointers, not components
        if Path(path).name.startswith("helmrepo"):
            continue
        components.setdefault(_component_of(path), []).append(path)

    for name, files in generators:
        components.setdefault(_generator_owner(name, files, components), []).extend(
            files
        )
    return components


def classify(
    drift_from_deployed: int, drift_from_validated: int, ci_state: str
) -> tuple[str, int]:
    """→ (status, commits ahead of what is deployed). Pure: unit-tested."""
    if drift_from_deployed == 0:
        return "deployed", 0
    if drift_from_validated == 0:
        # everything that drifted has already passed the full pipeline
        return "awaiting release", drift_from_deployed
    if ci_state == "failure":
        return "failed CI", drift_from_deployed
    return "validating", drift_from_deployed


def slugify(channel: str, component: str) -> str:
    """Badge filename. Channel-prefixed: a component can be in both."""
    name = component.removeprefix("apps/")
    return re.sub(r"[^a-z0-9]+", "-", f"{channel}-{name}".lower()).strip("-")


def write_badge(out: Path, slug: str, payload: dict[str, Any]) -> None:
    (out / f"{slug}.json").write_text(json.dumps(payload, ensure_ascii=False))


def label_for(component: str) -> str:
    """Badge label: the last path segment is what anyone calls the app."""
    return Path(component).name


def badge(label: str, status: str, ahead: int) -> dict[str, Any]:
    """One badge per component per channel — self-describing, since the README
    lists them as a bare wall with no surrounding text."""
    message = status if not ahead else f"{status} · {ahead}"
    return {
        "schemaVersion": 1,
        "label": label,
        "message": message,
        "color": COLORS.get(status, "lightgrey"),
        "cacheSeconds": 300,
    }


def af_image_paths() -> list[str]:
    """The purdue-af image's input paths, from the build's own definition —
    the same list that content-addresses the image, so drift here means CI
    would build a different image than the one core is pinned to."""
    out = subprocess.run(
        [str(REPO / IMAGE_INPUTS), "--paths", "purdue-af"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def af_image_version() -> str | None:
    text = (REPO / VALUES_YAML).read_text()
    m = re.search(r'docker_image_tag: "(\d+\.\d+\.\d+)"', text)
    return m.group(1) if m else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="directory to write badge JSON into")
    parser.add_argument(
        "--ci-state",
        default="unknown",
        choices=["success", "failure", "pending", "unknown"],
        help="ci-ok conclusion on main HEAD (splits 'validating' from 'failed CI')",
    )
    args = parser.parse_args()

    head = "origin/main" if ref_exists("origin/main") else "main"
    validated = (
        "origin/main-validated"
        if ref_exists("origin/main-validated")
        else "main-validated"
    )
    platform_tag = latest_platform_tag()
    if not ref_exists(validated):
        validated = head  # nothing published yet; treat main as the boundary

    rows: list[tuple[str, str, str, int]] = []

    for channel, deployed in (
        ("core", platform_tag),
        ("experimental", validated),
    ):
        if deployed is None:
            continue
        for component, paths in sorted(discover_components(channel).items()):
            drift = commits_touching(deployed, head, paths)
            unvalidated = commits_touching(validated, head, paths)
            status, ahead = classify(drift, unvalidated, args.ci_state)
            rows.append((channel, component, status, ahead))

    # the AF image ships on its own semver stream
    version = af_image_version()
    image_tag = f"v{version}" if version else None
    if image_tag and ref_exists(image_tag):
        paths = af_image_paths()
        drift = commits_touching(image_tag, head, paths)
        unvalidated = commits_touching(validated, head, paths)
        status, ahead = classify(drift, unvalidated, args.ci_state)
        rows.append(("core", "docker/purdue-af", status, ahead))

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for channel, component, status, ahead in rows:
            write_badge(
                args.out,
                slugify(channel, component),
                badge(label_for(component), status, ahead),
            )
        pending = sum(1 for row in rows if row[2] != "deployed")
        write_badge(
            args.out,
            "_pending",
            {
                "schemaVersion": 1,
                "label": "awaiting deployment",
                "message": "none" if not pending else f"{pending} components",
                "color": "brightgreen" if not pending else "blue",
                "cacheSeconds": 300,
            },
        )

    print(f"platform tag: {platform_tag} · main: {git('rev-parse', '--short', head)}\n")
    print("| channel | component | status | commits ahead |")
    print("| --- | --- | --- | --- |")
    for channel, component, status, ahead in rows:
        print(f"| {channel} | `{component}` | {status} | {ahead or ''} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
