#!/usr/bin/env python3
"""Per-component release status for the README dashboard.

Compares git refs alone, so it says what the ref behind each component holds,
never what the cluster runs. Badge legend: README.md; refs: RELEASING.md.

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
    "core": Path("flux/core-production/kustomization.yaml"),
    "experimental": Path("flux/experimental/kustomization.yaml"),
    "af-image": Path("flux/af-image/kustomization.yaml"),
}
IMAGE_INPUTS = Path(".github/workflows/image-inputs.sh")
CI_IMAGES_WORKFLOW = Path(".github/workflows/ci-images.yml")
VALUES_YAML = Path("apps/jupyterhub/jupyterhub/values.yaml")
AGENTIC_DEPLOYMENT = Path("apps/agentic-interface/deployment.yaml")

COLORS = {
    "released": "brightgreen",
    "awaiting release": "yellow",
    "validating": "blue",
    "failed CI": "red",
    "unknown": "lightgrey",
}


def ci_images() -> list[str]:
    """Images ci-images.yml builds: its own job for purdue-af, then the matrix."""
    workflow = yaml.safe_load((REPO / CI_IMAGES_WORKFLOW).read_text())
    matrix = workflow["jobs"]["build-aux-images"]["strategy"]["matrix"]["include"]
    return ["purdue-af", *(image["name"] for image in matrix)]


CI_IMAGES = ci_images()


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


def require_ref(ref: str) -> str:
    if not ref_exists(ref):
        sys.exit(f"component-status: missing git ref {ref}")
    return ref


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


def latest_image_tag() -> str | None:
    """Newest purdue-af image tag `vX.Y.Z` — what the af-image root tracks."""
    tags = [
        t
        for t in git("tag", "-l", "v*").splitlines()
        if re.fullmatch(r"v\d+\.\d+\.\d+", t)
    ]
    if not tags:
        return None
    return max(tags, key=lambda t: [int(p) for p in t[1:].split(".")])


VERSION_STAMP = re.compile(r"[+-]version = ")


def only_version_stamps(diff: str) -> bool:
    """True when every change in a `git show -U0` diff is a pixi.toml workspace
    version: bump-af-version.py's stamp, which image-inputs.sh does not hash."""
    sections = [s for s in re.split(r"(?m)^diff --git ", diff) if s.strip()]
    if not sections:
        return False
    for section in sections:
        header, hunk, body = section.partition("\n@@")
        if not hunk or not re.search(r"(?m)^\+\+\+ b/(.*/)?pixi\.toml$", header):
            return False
        for line in body.splitlines()[1:]:
            if line.startswith("@@") or line.startswith("\\"):
                continue
            if line[:1] in "+-" and not VERSION_STAMP.match(line):
                return False
    return True


def commits_touching(base: str, head: str, paths: Iterable[str]) -> int:
    """Number of commits in base..head that touch any of `paths`, version
    stamps aside."""
    paths = list(paths)
    out = git("log", "--format=%H", f"{base}..{head}", "--", *paths)
    shas = [line.strip() for line in out.splitlines() if line.strip()]
    return sum(
        1
        for sha in shas
        if not only_version_stamps(git("show", "--format=", "-U0", sha, "--", *paths))
    )


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

    Usually its own files say so, a component the channel deploys before one
    it only borrows a file from. When they all live outside apps/ (scripts
    and env manifests under docker/, pixi/ or workflows/), fall back to the
    component whose manifests mount it by name.
    """
    owners = {_component_of(f) for f in files if f.startswith("apps/")}
    if owners:
        return sorted(owners & components.keys() or owners)[0]
    for component, paths in components.items():
        for p in paths:
            manifest = REPO / p
            if manifest.suffix in (".yaml", ".yml") and manifest.is_file():
                if f"name: {name}\n" in manifest.read_text():
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
    drift_from_released: int, drift_from_validated: int, ci_state: str
) -> tuple[str, int]:
    """→ (status, commits ahead of what is released). Pure: unit-tested."""
    if drift_from_released == 0:
        return "released", 0
    if drift_from_validated == 0:
        # everything that drifted has already passed the full pipeline
        return "awaiting release", drift_from_released
    if ci_state == "failure":
        return "failed CI", drift_from_released
    return "validating", drift_from_released


def slugify(channel: str, component: str) -> str:
    """Badge filename. Channel-prefixed: a component can be in both."""
    name = component.removeprefix("apps/")
    return re.sub(r"[^a-z0-9]+", "-", f"{channel}-{name}".lower()).strip("-")


def write_badge(out: Path, slug: str, payload: dict[str, Any]) -> None:
    (out / f"{slug}.json").write_text(json.dumps(payload, ensure_ascii=False))


# The interLink apps are one per cluster, so they carry the node they register.
LABEL_OVERRIDES = {
    "apps/interlink/hammer": "interlink-hammer",
    "apps/interlink/nrp": "interlink-nrp",
    "apps/interlink/traefik": "interlink-traefik",
    "apps/af-utils/pixi-global-sync": "pixi-global",
}


def label_for(component: str) -> str:
    """Badge label: the last path segment is what anyone calls the app."""
    return LABEL_OVERRIDES.get(component, Path(component).name)


def badge(
    label: str, status: str, ahead: int, version: str | None = None
) -> dict[str, Any]:
    """One badge per component per channel — self-describing, since the README
    lists them as a bare wall with no surrounding text. Versioned images lead
    with their released version."""
    message = status if not ahead else f"{status} · {ahead}"
    if version:
        message = f"{version} · {message}"
    return {
        "schemaVersion": 1,
        "label": label,
        "message": message,
        "color": COLORS.get(status, "lightgrey"),
        "cacheSeconds": 300,
    }


def image_paths(name: str) -> list[str]:
    """An image's input paths, from the build's own definition — the same list
    that content-addresses it, so drift here means CI would build a different
    image than the one deployed."""
    out = subprocess.run(
        [str(REPO / IMAGE_INPUTS), "--paths", name],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def af_image_version(platform_tag: str) -> str | None:
    """The hub tracks the platform tag, so the pin on `main` is not yet released."""
    text = git("show", f"{platform_tag}:{VALUES_YAML}")
    m = re.search(r'docker_image_tag: "(\d+\.\d+\.\d+)"', text)
    return m.group(1) if m else None


def agentic_interface_version() -> str | None:
    """The version the agentic-interface Deployment pins."""
    text = (REPO / AGENTIC_DEPLOYMENT).read_text()
    m = re.search(r"/agentic-interface:(\d+\.\d+\.\d+)\s*$", text, re.MULTILINE)
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
    validated = require_ref("origin/main-validated")
    platform_tag = latest_platform_tag()
    if platform_tag is None:
        sys.exit("component-status: missing git ref: no platform tag YYYY.M.SEQ")
    image_tag = latest_image_tag()
    if image_tag is None:
        sys.exit("component-status: missing git ref: no image tag vX.Y.Z")

    rows: list[tuple[str, str, str, int]] = []

    for channel, released in (
        ("core", platform_tag),
        ("experimental", validated),
        ("af-image", image_tag),
    ):
        for component, paths in sorted(discover_components(channel).items()):
            drift = commits_touching(released, head, paths)
            unvalidated = commits_touching(validated, head, paths)
            status, ahead = classify(drift, unvalidated, args.ci_state)
            rows.append((channel, component, status, ahead))

    # images are their own stream, not part of either Flux channel
    af_version = af_image_version(platform_tag)
    agentic_version = agentic_interface_version()
    if not af_version or not agentic_version:
        sys.exit(
            f"component-status: no version pin: {VALUES_YAML} at {platform_tag} "
            f"gives {af_version}, {AGENTIC_DEPLOYMENT} gives {agentic_version}"
        )
    release_tags = {
        "purdue-af": require_ref(f"v{af_version}"),
        "agentic-interface": require_ref(f"agentic-interface-v{agentic_version}"),
    }
    versions = {
        ("image", "purdue-af"): af_version,
        ("image", "agentic-interface"): agentic_version,
    }
    for channel, component, _, _ in rows:
        if channel == "af-image":
            versions[(channel, component)] = image_tag.removeprefix("v")
    for name in CI_IMAGES:
        released_ref = release_tags.get(name, validated)
        paths = image_paths(name)
        drift = commits_touching(released_ref, head, paths)
        unvalidated = commits_touching(validated, head, paths)
        status, ahead = classify(drift, unvalidated, args.ci_state)
        rows.append(("image", name, status, ahead))

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for channel, component, status, ahead in rows:
            write_badge(
                args.out,
                slugify(channel, component),
                badge(
                    label_for(component),
                    status,
                    ahead,
                    versions.get((channel, component)),
                ),
            )
        unreleased = sum(1 for row in rows if row[2] != "released")
        write_badge(
            args.out,
            "_pending",
            {
                "schemaVersion": 1,
                "label": "unreleased",
                "message": "none" if not unreleased else f"{unreleased} components",
                "color": "brightgreen" if not unreleased else "blue",
                "cacheSeconds": 300,
            },
        )

    print(
        f"platform tag: {platform_tag} · image tag: {image_tag} · "
        f"main: {git('rev-parse', '--short', head)}\n"
    )
    print("| channel | component | status | commits ahead |")
    print("| --- | --- | --- | --- |")
    for channel, component, status, ahead in rows:
        print(f"| {channel} | `{component}` | {status} | {ahead or ''} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
