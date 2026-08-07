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


def _extra_config() -> str:
    """The gateway's extraConfig, dedented — it is Python, not YAML."""
    doc = yaml.safe_load(_values_text())
    return doc["gateway"]["extraConfig"]["config"]


def _interlink_clusters():
    """Evaluate the gateway's own INTERLINK_CLUSTERS table, so the test reads
    what the gateway will actually run rather than a copy of it."""
    src = _extra_config()
    start = src.index("INTERLINK_CLUSTERS = {")
    ns: dict = {}
    exec(src[start : src.index("\n}", start) + 2], ns)
    return ns["INTERLINK_CLUSTERS"]


def test_interlink_sbatch_targets():
    clusters = _interlink_clusters()
    assert set(clusters) == {"hammer", "gautschi", "negishi"}
    assert (
        clusters["hammer"]["sbatch_flags"] == "--account=cms --partition=hammer-nodes"
    )
    assert (
        clusters["gautschi"]["sbatch_flags"]
        == "--account=cms --partition=cpu --qos=standby"
    )
    # Negishi mirrors Gautschi apart from the account it charges against
    assert (
        clusters["negishi"]["sbatch_flags"]
        == "--account=cms-a --partition=cpu --qos=standby"
    )


def test_every_interlink_cluster_has_a_deployed_node():
    """A cluster offered here with no interLink node would accept the option and
    then leave every worker Pending."""
    nodes = {c["node"] for c in _interlink_clusters().values()}
    deployed = (REPO / "deploy/experimental/kustomization.yaml").read_text()
    for node in nodes:
        cluster = node.removeprefix("interlink-")
        assert f"apps/interlink/{cluster}/helmrelease.yaml" in deployed, node
        assert f"# - ../../apps/interlink/{cluster}/" not in deployed, (
            f"{node} is offered but its node is commented out"
        )


def test_cluster_choices_are_derived_not_hardcoded():
    """The Select list, the label and both error messages must come from
    INTERLINK_CLUSTERS, or adding a cluster leaves them silently stale."""
    text = _extra_config()
    assert '["geddes"] + list(INTERLINK_CLUSTERS)' in text
    for stale in ("hammer, or gautschi", "(hammer/gautschi)", "hammer or gautschi"):
        assert stale not in text, f"hardcoded cluster list: {stale!r}"
