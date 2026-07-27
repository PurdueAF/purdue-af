"""Tests for .github/workflows/component-status.py (the README deployment
dashboard). Runs against the REAL deploy kustomizations and the REAL README,
so a component added to a channel without a badge — or a badge pointing at a
component that no longer exists — fails here instead of quietly rendering
`resource not found` in the README."""

import json

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
    "deployed_drift,validated_drift,ci_state,expected",
    [
        # nothing moved since the deploy — CI state is irrelevant
        (0, 0, "success", "deployed"),
        (0, 0, "failure", "deployed"),
        # drift, but all of it is past the validation boundary
        (3, 0, "pending", "awaiting release"),
        (3, 0, "failure", "awaiting release"),
        # drift that has not been validated yet
        (3, 2, "pending", "validating"),
        (3, 2, "unknown", "validating"),
        (3, 2, "failure", "failed CI"),
        # a component can be behind the boundary only if it is behind the
        # deploy too, so unvalidated drift never outranks total drift
        (1, 1, "success", "validating"),
    ],
)
def test_classify(cs, deployed_drift, validated_drift, ci_state, expected):
    status, ahead = cs.classify(deployed_drift, validated_drift, ci_state)
    assert status == expected
    assert ahead == (0 if expected == "deployed" else deployed_drift)


def test_every_status_has_a_colour(cs):
    for state in ("success", "failure", "pending", "unknown"):
        for drift in range(3):
            status, _ = cs.classify(drift, drift, state)
            assert status in cs.COLORS


# --- component discovery --------------------------------------------------


def test_channels_resolve_to_real_kustomizations(cs):
    for path in cs.CHANNELS.values():
        assert (REPO / path).is_file()


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


def test_commented_out_resources_are_excluded(cs):
    """The experimental kustomization keeps thanos commented out; a text-based
    parser would report it as a live component."""
    experimental = cs.discover_components("experimental")
    assert "apps/monitoring/thanos" not in experimental
    assert (REPO / "apps/monitoring/thanos").is_dir()  # still on disk


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


def test_generator_owner_falls_back_to_its_own_name(cs):
    """An unattached ConfigMap still gets a row rather than vanishing."""
    owner = cs._generator_owner("orphan-config", ["docker/orphan/x.py"], {})
    assert owner == "orphan-config"


# --- badges ---------------------------------------------------------------


def test_slugs_are_unique_across_channels(cs, components):
    """apps/infrastructure and af-monitoring are deployed in BOTH channels;
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


# --- README ---------------------------------------------------------------


def test_readme_references_every_component_badge(cs, components):
    """The README badge list is static markdown; this keeps it honest."""
    readme = README.read_text()
    expected = {
        cs.slugify(channel, component)
        for channel, mapping in components.items()
        for component in mapping
    }
    expected |= {f"image-{name}" for name in cs.CI_IMAGES}

    missing = sorted(s for s in expected if f"[{s}]:" not in readme)
    assert not missing, f"add these badges to README.md: {missing}"


def test_readme_has_no_badges_for_dead_components(cs, components):
    """A removed component must lose its badge, or the README renders an
    error image forever (nothing ever writes that JSON again)."""
    readme = README.read_text()
    live = {
        cs.slugify(channel, component)
        for channel, mapping in components.items()
        for component in mapping
    }
    live |= {f"image-{name}" for name in cs.CI_IMAGES} | {"status-pending"}

    referenced = {
        line.split("]:")[0].lstrip("[")
        for line in readme.splitlines()
        if line.startswith("[") and "/status/badges/" in line
    }
    assert referenced - live == set()


def test_label_overrides_point_at_real_components(cs, components):
    """A renamed or removed component must not leave a dangling override."""
    live = {c for mapping in components.values() for c in mapping}
    assert set(cs.LABEL_OVERRIDES) <= live


# --- images ---------------------------------------------------------------


def test_ci_images_match_the_build_workflow(cs):
    """The image list is hand-written; if ci-images.yml gains or drops an
    image, the dashboard would silently stop covering it."""
    workflow = yaml.safe_load((REPO / ".github/workflows/ci-images.yml").read_text())
    jobs = workflow["jobs"]
    aux = {m["name"] for m in jobs["build-aux-images"]["strategy"]["matrix"]["include"]}
    assert "build-af-image" in jobs  # purdue-af gets a job of its own
    assert set(cs.CI_IMAGES) == aux | {"purdue-af"}


@pytest.mark.parametrize("name", ["purdue-af", "agentic-interface"])
def test_image_paths_come_from_the_build_definition(cs, name):
    paths = cs.image_paths(name)
    assert paths, name
    # image-inputs.sh always folds in the build logic itself
    assert ".github/workflows/image-inputs.sh" in paths


# --- version streams ------------------------------------------------------


def test_af_image_version_is_read_from_values_yaml(cs):
    version = cs.af_image_version()
    assert version is not None, "docker_image_tag no longer matches"
    assert version in (REPO / cs.VALUES_YAML).read_text()


def test_latest_platform_tag_is_calver(cs):
    tag = cs.latest_platform_tag()
    if tag is None:
        pytest.skip("no platform tags in this checkout")
    year, month, seq = (int(p) for p in tag.split("."))
    assert year >= 2024 and 1 <= month <= 12 and seq >= 0
