"""Fixtures for manifest tests — plain YAML parsing, no cluster.

These suites assert the wiring that only fails at deploy time: a patch that
matches nothing, a ConfigMap name nobody generates, a placeholder Flux
silently blanks out.
"""

import pytest
import yaml
from common import REPO

INTERLINK = REPO / "apps" / "interlink"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"


def load(path):
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="session")
def interlink_clusters():
    """{cluster: {"values": ..., "release": ..., "dir": ...}} for every
    apps/interlink/<cluster>/ that has a HelmRelease."""
    clusters = {}
    for d in sorted(INTERLINK.iterdir()):
        release = d / "helmrelease.yaml"
        if not d.is_dir() or not release.is_file():
            continue
        clusters[d.name] = {
            "dir": d,
            "release": load(release),
            "values": load(d / "values.yaml"),
        }
    assert clusters, "no interlink clusters found"
    return clusters


@pytest.fixture(scope="session")
def active_clusters(interlink_clusters, experimental):
    """Only the clusters the experimental channel actually deploys — the rest
    are commented out in the kustomization, waiting on their munge PVC."""
    resources = set(experimental["resources"])
    return {
        cluster: app
        for cluster, app in interlink_clusters.items()
        if f"../../apps/interlink/{cluster}/helmrelease.yaml" in resources
    }


@pytest.fixture(scope="session")
def experimental():
    return load(EXPERIMENTAL)


@pytest.fixture(scope="session")
def experimental_text():
    return EXPERIMENTAL.read_text()
