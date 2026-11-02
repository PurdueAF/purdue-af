"""Fixtures for manifest tests — plain YAML parsing, no cluster.

These suites assert the wiring that only fails at deploy time: a ConfigMap
name nobody generates, a placeholder Flux silently blanks out.
"""

import pytest
import yaml
from common import REPO

INTERLINK = REPO / "apps" / "interlink"
EXPERIMENTAL = REPO / "flux" / "experimental" / "kustomization.yaml"


def load(path):
    return yaml.safe_load(path.read_text())


def merged(base, override):
    """Flux's merge of valuesFrom: maps deep; lists and nulls replace."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merged(out[key], value)
        else:
            out[key] = value
    return out


@pytest.fixture(scope="session")
def interlink_clusters():
    """{cluster: {"values": ..., "release": ..., "dir": ...}} for every
    apps/interlink/<cluster>/ whose HelmRelease is the interlink chart."""
    shared = load(INTERLINK / "values.yaml")
    clusters = {}
    for d in sorted(INTERLINK.iterdir()):
        release = d / "helmrelease.yaml"
        if not d.is_dir() or not release.is_file():
            continue
        if load(release)["spec"]["chart"]["spec"]["chart"] != "interlink":
            continue
        clusters[d.name] = {
            "dir": d,
            "release": load(release),
            "values": merged(shared, load(d / "values.yaml")),
        }
    assert clusters, "no interlink clusters found"
    return clusters


@pytest.fixture(scope="session")
def experimental():
    return load(EXPERIMENTAL)
