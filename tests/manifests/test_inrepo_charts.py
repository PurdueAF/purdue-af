"""Charts that live in this repository must be reconciled by Git revision.

Flux packages a chart sourced from a GitRepository under the version string in
its `Chart.yaml`, and the default `reconcileStrategy: ChartVersion` re-packages
only when that string changes. Our in-repo charts keep a static version, so
template edits never reached the cluster — while values edits, which land
through a kustomize-generated ConfigMap, triggered an upgrade regardless.

That combination is worse than either failure alone: the release runs NEW
values against the OLD chart. It cost a day on 2026-09-07, when Triton's
`--http-port=8100` (values) met a `containerPort: 8000` (chart) and the
kubelet's probe hit Ray's proxy instead of Triton, killing the container every
four minutes.
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
