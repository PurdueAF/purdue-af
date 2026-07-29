"""Tests for apps/dask-gateway/dask-gateway-k8s-interlink."""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
INTERLINK_GW = REPO / "apps" / "dask-gateway" / "dask-gateway-k8s-interlink"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"


def _values_text() -> str:
    return (INTERLINK_GW / "values.yaml").read_text()


def test_flux_deploys_interlink_gateway():
    text = EXPERIMENTAL.read_text()
    assert "../../apps/dask-gateway/dask-gateway-k8s-interlink/helmrelease.yaml" in text
    assert "../../apps/dask-gateway/dask-gateway-k8s-interlink/ingress.yaml" in text
    # Not commented out
    for line in text.splitlines():
        if "dask-gateway-k8s-interlink/helmrelease.yaml" in line:
            assert not line.strip().startswith("#"), line


def test_cluster_option_defaults_to_geddes():
    text = _values_text()
    assert (
        'Select(\n              "cluster"' in text
        or 'Select(\n          "cluster"' in text
        or '"cluster"' in text
    )
    assert 'default="geddes"' in text
    assert '"hammer"' in text and '"gautschi"' in text


def test_interlink_rejects_non_purdue_and_requires_depot_or_cvmfs():
    text = _values_text()
    assert "Purdue accounts only" in text
    assert "_require_depot_or_cvmfs" in text
    assert (
        'startswith(("/depot/", "/cvmfs/"))' in text
        or 'allowed = ("/depot/", "/cvmfs/")' in text
    )


def test_longer_timeouts_for_slurm_queue():
    text = _values_text()
    assert "cluster_start_timeout = 900" in text
    assert "worker_start_timeout = 900" in text
    assert "idle_timeout = 86400" in text


def test_helmrelease_mounts_depot_for_pixi_validation():
    hr = yaml.safe_load((INTERLINK_GW / "helmrelease.yaml").read_text())
    patches = hr["spec"]["postRenderers"][0]["kustomize"]["patches"]
    api_patch = next(p for p in patches if p["target"]["kind"] == "Deployment")
    assert "datadepot.rcac.purdue.edu" in api_patch["patch"]
    assert "/depot/cms" in api_patch["patch"]


def test_ingress_host():
    ing = yaml.safe_load((INTERLINK_GW / "ingress.yaml").read_text())
    assert (
        ing["spec"]["rules"][0]["host"]
        == "dask-gateway-k8s-interlink.geddes.rcac.purdue.edu"
    )


def test_partitions_match_interlink_test_pods():
    text = _values_text()
    assert '"partition": "hammer-nodes"' in text
    assert '"partition": "cpu"' in text
    assert "interlink-hammer" in text
    assert "interlink-gautschi" in text
