"""Charts that live in this repository must be reconciled by Git revision.

The default `reconcileStrategy: ChartVersion` re-packages only when the
`Chart.yaml` version changes, which in-repo charts never bump, while values
edits still upgrade the release: new values would run against a stale chart.
"""

import yaml
from common import REPO

APPS = REPO / "apps"


def in_repo_chart_releases():
    """Every HelmRelease whose chart is a path in this repository."""
    found = []
    for path in sorted(APPS.rglob("helmrelease*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") != "HelmRelease":
                continue
            spec = doc["spec"].get("chart", {}).get("spec", {})
            if str(spec.get("chart", "")).startswith("./"):
                found.append((path.relative_to(REPO), spec))
    return found


def test_there_are_in_repo_charts_to_check():
    """A rename that empties this list must not silently pass the test below."""
    assert len(in_repo_chart_releases()) >= 2


def test_in_repo_charts_reconcile_on_revision():
    for path, spec in in_repo_chart_releases():
        assert spec.get("reconcileStrategy") == "Revision", (
            f"{path}: chart {spec['chart']} lives in this repository, so its "
            "HelmRelease needs reconcileStrategy: Revision — otherwise Flux "
            "keeps the chart it packaged under the same Chart.yaml version and "
            "renders it against newer values."
        )


def test_in_repo_charts_are_sourced_from_a_gitrepository():
    """ChartVersion would at least be defensible for a registry chart; the
    strategy above only makes sense because the source is Git."""
    for path, spec in in_repo_chart_releases():
        assert spec["sourceRef"]["kind"] == "GitRepository", path
