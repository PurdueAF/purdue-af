"""Git-sourced charts must be reconciled by Git revision.

The default `reconcileStrategy: ChartVersion` re-packages only when the
`Chart.yaml` version changes, which a Git source need not bump, while values
edits still upgrade the release: new values would run against a stale chart.
"""

import yaml
from common import REPO

APPS = REPO / "apps"


def git_sourced_releases():
    """Every HelmRelease whose chart comes from a GitRepository."""
    found = []
    for path in sorted(APPS.rglob("helmrelease*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") != "HelmRelease":
                continue
            spec = doc["spec"].get("chart", {}).get("spec", {})
            if spec.get("sourceRef", {}).get("kind") == "GitRepository":
                found.append((path.relative_to(REPO), spec))
    return found


def test_there_are_git_sourced_charts_to_check():
    """A rename that empties this list must not silently pass the test below."""
    assert git_sourced_releases()


def test_git_sourced_charts_reconcile_on_revision():
    for path, spec in git_sourced_releases():
        assert spec.get("reconcileStrategy") == "Revision", (
            f"{path}: chart {spec['chart']} comes from a GitRepository, so its "
            "HelmRelease needs reconcileStrategy: Revision — otherwise Flux "
            "keeps the chart it packaged under the same Chart.yaml version and "
            "renders it against newer values."
        )
