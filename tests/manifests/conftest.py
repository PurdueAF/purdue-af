"""Fixtures for manifest tests — plain YAML parsing, no cluster.

These suites assert the wiring that only fails at deploy time: a ConfigMap
name nobody generates, a placeholder Flux silently blanks out.
"""

import pytest
from common import EXPERIMENTAL, REPO, load_yaml

INTERLINK = REPO / "apps" / "interlink"


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
    shared = load_yaml(INTERLINK / "values.yaml")
    clusters = {}
    for d in sorted(INTERLINK.iterdir()):
        release = d / "helmrelease.yaml"
        if not d.is_dir() or not release.is_file():
            continue
        if load_yaml(release)["spec"]["chart"]["spec"]["chart"] != "interlink":
            continue
        clusters[d.name] = {
            "dir": d,
            "release": load_yaml(release),
            "values": merged(shared, load_yaml(d / "values.yaml")),
        }
    assert clusters, "no interlink clusters found"
    return clusters


@pytest.fixture(scope="session")
def experimental():
    return load_yaml(EXPERIMENTAL)
