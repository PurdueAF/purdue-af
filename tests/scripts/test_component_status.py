"""Tests for .github/workflows/component-status.py (the README status
dashboard). Runs against the REAL Flux root kustomizations and the README badge
list, whose `[slug]:` link definitions must match the components the script
publishes badges for."""

import json
import re
from pathlib import Path

import pytest
import yaml
from common import REPO, load_script

SCRIPT_PATH = REPO / ".github" / "workflows" / "component-status.py"
README = REPO / "README.md"


@pytest.fixture(scope="session")
def cs():
    return load_script(SCRIPT_PATH, "component_status")


@pytest.fixture(scope="session")
def components(cs):
    return {channel: cs.discover_components(channel) for channel in cs.CHANNELS}


# --- classification -------------------------------------------------------
#
# The whole dashboard hangs off this function: everything else just feeds it
# commit counts.


@pytest.mark.parametrize(
    "released_drift,validated_drift,ci_state,expected",
    [
        # nothing moved since the release — CI state is irrelevant
        (0, 0, "success", "released"),
        (0, 0, "failure", "released"),
        # drift, but all of it is past the validation boundary
        (3, 0, "pending", "awaiting release"),
        (3, 0, "failure", "awaiting release"),
        # drift that has not been validated yet
        (3, 2, "pending", "validating"),
        (3, 2, "unknown", "validating"),
        (3, 2, "failure", "failed CI"),
        # a component can be behind the boundary only if it is behind the
        # release too, so unvalidated drift never outranks total drift
        (1, 1, "success", "validating"),
    ],
)
def test_classify(cs, released_drift, validated_drift, ci_state, expected):
    status, ahead = cs.classify(released_drift, validated_drift, ci_state)
    assert status == expected
    assert ahead == (0 if expected == "released" else released_drift)


def test_every_status_has_a_colour(cs):
    for state in ("success", "failure", "pending", "unknown"):
        for drift in range(3):
            status, _ = cs.classify(drift, drift, state)
            assert status in cs.COLORS


# --- component discovery --------------------------------------------------


def test_discovered_paths_all_exist(components):
    """Catches a renamed directory: the kustomization would still list the old
    path, and `git log -- <path>` reports 0 commits for it — silent green."""
    for channel, mapping in components.items():
        for component, paths in mapping.items():
            for path in paths:
                assert (REPO / path).exists(), f"{channel}/{component}: {path}"


def test_components_are_not_too_deep(cs, components):
    """extraFiles/ and dashboards/ belong to the component above them."""
    for mapping in components.values():
        for component in mapping:
            assert len(component.split("/")) <= 3, component


def test_commented_out_resources_are_excluded(cs, tmp_path, monkeypatch):
    """A text-based parser would report a commented-out resource as live."""
    root = tmp_path.resolve()
    (root / "flux").mkdir()
    (root / "flux/kustomization.yaml").write_text(
        "resources:\n"
        "  - ../apps/live/helmrelease.yaml\n"
        "  # - ../apps/parked/helmrelease.yaml\n"
    )
    monkeypatch.setattr(cs, "REPO", root)
    resources, _ = cs._read_kustomization(Path("flux/kustomization.yaml"))
    assert resources == ["apps/live/helmrelease.yaml"]


def test_helm_repositories_are_not_components(components):
    """A HelmRepository is a source pointer; drift in one means nothing."""
    for mapping in components.values():
        for paths in mapping.values():
            for path in paths:
                assert not path.split("/")[-1].startswith("helmrepo")


def test_configmap_files_join_their_owning_component(cs, components):
    """Generator inputs from outside apps/ must not become phantom components:
    the pixi global manifests are an input of the daemon that applies them,
    and the node healthcheck script belongs to af-monitoring."""
    experimental = components["experimental"]

    assert "pixi" not in experimental
    assert "pixi/global" not in experimental
    sync_inputs = experimental["apps/af-utils/pixi-global-sync"]
    assert "pixi/global/pixi.lock" in sync_inputs

    assert "docker/af-node-monitor" not in experimental
    monitoring = experimental["apps/monitoring/af-monitoring"]
    assert any(p.startswith("docker/af-node-monitor/") for p in monitoring)


def test_a_borrowed_file_makes_no_component_of_its_owner(cs):
    """The Ray gateway's ConfigMap carries the Hub's gpu_queries.py."""
    owner = cs._generator_owner(
        "ray-train-gateway",
        ["apps/ray-train/gateway.py", "apps/jupyterhub/jupyterhub/gpu_queries.py"],
        {"apps/ray-train": ["apps/ray-train/deployment.yaml"]},
    )
    assert owner == "apps/ray-train"


def test_generator_owner_falls_back_to_its_own_name(cs):
    """An unattached ConfigMap still gets a row rather than vanishing."""
    owner = cs._generator_owner("orphan-config", ["docker/orphan/x.py"], {})
    assert owner == "orphan-config"


# --- badges ---------------------------------------------------------------


def test_slugs_are_unique_across_channels(cs, components):
    """apps/storage and af-monitoring are deployed in BOTH channels;
    colliding slugs would make one overwrite the other's badge."""
    slugs = [
        cs.slugify(channel, component)
        for channel, mapping in components.items()
        for component in mapping
    ]
    assert len(slugs) == len(set(slugs))


@pytest.mark.parametrize("ahead", [0, 1, 42])
def test_badge_matches_the_shields_endpoint_schema(cs, ahead):
    payload = cs.badge("core", "awaiting release", ahead)
    assert json.loads(json.dumps(payload)) == payload
    assert payload["schemaVersion"] == 1
    assert payload["label"] == "core"  # the table names the component
    assert payload["color"] == cs.COLORS["awaiting release"]
    assert payload["message"] == (
        "awaiting release" if not ahead else f"awaiting release · {ahead}"
    )
    assert payload["cacheSeconds"] >= 300  # shields' floor for endpoint badges


def test_badge_colour_falls_back_for_an_unknown_status(cs):
    assert cs.badge("x", "something new", 0)["color"] == "lightgrey"


# --- labels ---------------------------------------------------------------


def _readme_badge_slugs():
    """Slugs named by the README's `[slug]: .../status/badges/...` link definitions."""
    return {
        line.split("]:")[0].lstrip("[")
        for line in README.read_text().splitlines()
        if line.startswith("[") and "/status/badges/" in line
    }


def _live_slugs(cs, components):
    live = {
        cs.slugify(channel, component)
        for channel, mapping in components.items()
        for component in mapping
    }
    return live | {f"image-{name}" for name in cs.CI_IMAGES}


def test_readme_links_a_badge_for_every_component(cs, components):
    missing = sorted(_live_slugs(cs, components) - _readme_badge_slugs())
    assert not missing, f"add these badges to README.md: {missing}"


def test_readme_links_no_badge_for_a_dead_component(cs, components):
    """Nothing writes a dead slug's JSON again, so its badge renders an error forever."""
    dead = _readme_badge_slugs() - _live_slugs(cs, components) - {"status-pending"}
    assert dead == set(), f"remove these badges from README.md: {sorted(dead)}"


def test_label_overrides_point_at_real_components(cs, components):
    """A renamed or removed component must not leave a dangling override."""
    live = {c for mapping in components.values() for c in mapping}
    assert set(cs.LABEL_OVERRIDES) <= live


# --- images ---------------------------------------------------------------


def test_image_paths_come_from_the_build_definition(cs):
    for name in cs.CI_IMAGES:
        paths = cs.image_paths(name)
        assert any(path.startswith(f"docker/{name}") for path in paths), name
        # image-inputs.sh always folds in the build logic itself
        assert ".github/workflows/image-inputs.sh" in paths


# --- version streams ------------------------------------------------------


def test_af_image_version_is_read_at_the_platform_tag(cs, monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args)
        return (REPO / cs.VALUES_YAML).read_text()

    monkeypatch.setattr(cs, "git", fake_git)
    assert cs.af_image_version("2026.9.1") is not None
    assert calls == [("show", "2026.9.1:apps/jupyterhub/jupyterhub/values.yaml")]


def test_agentic_version_is_read_from_the_deployment(cs):
    """Both release scripts must read the same pin off the real manifest."""
    bump = load_script(
        REPO / ".github" / "workflows" / "bump-agentic-version.py",
        "bump_agentic_version",
    )
    text = (REPO / cs.AGENTIC_DEPLOYMENT).read_text()
    assert cs.agentic_interface_version() == bump.current_version(text)


@pytest.mark.parametrize("tag,expected", [("1.4.2", "1.4.2"), ("1.4.2-rc1", None)])
def test_agentic_version_parsing(cs, monkeypatch, tmp_path, tag, expected):
    manifest = tmp_path / cs.AGENTIC_DEPLOYMENT
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"        image: reg.example/purdueaf/agentic-interface:{tag}\n"
    )
    monkeypatch.setattr(cs, "REPO", tmp_path)
    assert cs.agentic_interface_version() == expected


def test_versioned_badge_leads_with_the_version(cs):
    payload = cs.badge("agentic-interface", "released", 0, "1.2.3")
    assert payload["message"] == "1.2.3 · released"
    payload = cs.badge("agentic-interface", "awaiting release", 2, "1.2.3")
    assert payload["message"] == "1.2.3 · awaiting release · 2"
    # unversioned badges are unchanged
    assert cs.badge("x", "released", 0)["message"] == "released"


def test_latest_platform_tag_orders_numerically(cs, monkeypatch):
    """2026.10.1 is newer than 2026.9.5; non-CalVer tags are ignored."""
    tags = "2026.9.5\n2026.10.1\n2026.2.30\n2x-not-calver\n2026.11\n"
    monkeypatch.setattr(cs, "git", lambda *a: tags)
    assert cs.latest_platform_tag() == "2026.10.1"
    monkeypatch.setattr(cs, "git", lambda *a: "")
    assert cs.latest_platform_tag() is None


def test_commits_touching_counts_nonblank_lines(cs, monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args)
        return "abc one\n\ndef two\n"

    monkeypatch.setattr(cs, "git", fake_git)
    assert cs.commits_touching("base", "head", ["apps/x", "docker/y"]) == 2
    assert calls == [("log", "--oneline", "base..head", "--", "apps/x", "docker/y")]


def test_paths_outside_the_repo_are_dropped(cs, monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / "flux").mkdir(parents=True)
    (repo / "flux" / "k.yaml").write_text(
        yaml.safe_dump(
            {
                "resources": ["../apps/a/hr.yaml", "../../elsewhere.yaml"],
                "configMapGenerator": [
                    {"name": "only-outside", "files": ["x=../../outside.py"]},
                    {"name": "a-config", "files": ["v.yaml=../apps/a/values.yaml"]},
                ],
            }
        )
    )
    monkeypatch.setattr(cs, "REPO", repo)
    resources, generators = cs._read_kustomization(cs.Path("flux/k.yaml"))
    assert resources == ["apps/a/hr.yaml"]
    assert generators == [("a-config", ["apps/a/values.yaml"])]


# --- main -----------------------------------------------------------------


@pytest.fixture()
def fake_repo(cs, monkeypatch):
    """Refs, components and commit counts for main(), no git involved."""
    refs = {
        "origin/main",
        "origin/main-validated",
        "2026.9.1",
        "v0.13.7",
        "agentic-interface-v0.2.7",
    }
    components = {
        "core": {"apps/storage": ["apps/storage/pvc.yaml"]},
        "experimental": {
            "apps/storage": ["apps/storage/pvc.yaml"],
            "apps/interlink/hammer": ["apps/interlink/hammer/values.yaml"],
        },
    }
    # (released ref, first path) -> commits; unlisted pairs are 0
    drift = {
        ("2026.9.1", "apps/storage/pvc.yaml"): 2,
        ("origin/main-validated", "apps/interlink/hammer/values.yaml"): 1,
        ("v0.13.7", "docker/purdue-af"): 3,
        ("origin/main-validated", "docker/af-pod-monitor"): 1,
    }
    monkeypatch.setattr(cs, "ref_exists", lambda ref: ref in refs)
    monkeypatch.setattr(
        cs, "latest_platform_tag", lambda: "2026.9.1" if "2026.9.1" in refs else None
    )
    monkeypatch.setattr(cs, "discover_components", lambda ch: components[ch])
    monkeypatch.setattr(cs, "image_paths", lambda name: [f"docker/{name}"])
    monkeypatch.setattr(cs, "af_image_version", {"2026.9.1": "0.13.7"}.get)
    monkeypatch.setattr(cs, "agentic_interface_version", lambda: "0.2.7")
    monkeypatch.setattr(
        cs, "commits_touching", lambda base, head, paths: drift.get((base, paths[0]), 0)
    )
    monkeypatch.setattr(cs, "git", lambda *a: "abc1234")
    return refs


def run_main(cs, monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["component-status.py", *map(str, argv)])
    return cs.main()


def read_badges(out):
    return {p.stem: json.loads(p.read_text()) for p in out.glob("*.json")}


def test_main_writes_a_badge_per_row(cs, fake_repo, monkeypatch, tmp_path, capsys):
    out = tmp_path / "badges"
    assert run_main(cs, monkeypatch, "--out", out, "--ci-state", "failure") == 0
    badges = read_badges(out)

    assert badges["core-storage"]["message"] == "awaiting release · 2"
    assert badges["experimental-storage"]["message"] == "released"
    hammer = badges["experimental-interlink-hammer"]
    assert (hammer["label"], hammer["message"]) == ("interlink-hammer", "failed CI · 1")
    assert badges["image-purdue-af"]["message"] == "0.13.7 · awaiting release · 3"
    assert badges["image-agentic-interface"]["message"] == "0.2.7 · released"
    assert badges["image-af-pod-monitor"]["message"] == "failed CI · 1"
    assert set(badges) >= {f"image-{name}" for name in cs.CI_IMAGES}
    assert badges["_pending"]["label"] == "unreleased"
    assert badges["_pending"]["message"] == "4 components"
    assert badges["_pending"]["color"] == "blue"

    table = capsys.readouterr().out
    assert "platform tag: 2026.9.1 · main: abc1234" in table
    assert "| image | `purdue-af` | awaiting release | 3 |" in table
    assert "| experimental | `apps/storage` | released |  |" in table


def test_main_reports_nothing_unreleased(cs, fake_repo, monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "commits_touching", lambda base, head, paths: 0)
    out = tmp_path / "badges"
    run_main(cs, monkeypatch, "--out", out)
    pending = read_badges(out)["_pending"]
    assert (pending["message"], pending["color"]) == ("none", "brightgreen")


@pytest.mark.parametrize(
    "ref,named",
    [
        ("origin/main-validated", "origin/main-validated"),
        ("2026.9.1", "platform tag"),
        ("v0.13.7", "v0.13.7"),
        ("agentic-interface-v0.2.7", "agentic-interface-v0.2.7"),
    ],
)
def test_main_fails_naming_a_missing_ref(cs, fake_repo, monkeypatch, ref, named):
    fake_repo.discard(ref)
    with pytest.raises(SystemExit, match=re.escape(named)):
        run_main(cs, monkeypatch)


@pytest.mark.parametrize("reader", ["af_image_version", "agentic_interface_version"])
def test_main_fails_without_a_version_pin(cs, fake_repo, monkeypatch, reader):
    monkeypatch.setattr(cs, reader, lambda *a: None)
    with pytest.raises(SystemExit, match="no version pin"):
        run_main(cs, monkeypatch)


def test_main_without_out_only_prints(cs, fake_repo, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    run_main(cs, monkeypatch)
    assert not list(tmp_path.iterdir())
    assert "| channel | component | status | commits ahead |" in capsys.readouterr().out


def test_generator_owner_is_the_component_mounting_it_by_name(cs):
    """af-node-monitor's ConfigMap is built from docker/ only; the probe
    DaemonSet under apps/monitoring/af-monitoring names it, so it belongs there."""
    owner = cs._generator_owner(
        "af-node-monitor-config",
        ["docker/af-node-monitor/node_healthcheck.py"],
        {
            "apps/monitoring/af-monitoring": [
                "apps/monitoring/af-monitoring/daemonset-af-node-probe.yaml"
            ]
        },
    )
    assert owner == "apps/monitoring/af-monitoring"
