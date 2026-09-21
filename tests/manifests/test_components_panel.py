"""The Geddes storage row reports how badly mounts are failing, not that they are.

The Components panel paints 0 red, anything between 0 and 1 orange, and 1
green. A row that collapses "at least one mount failed" onto a single constant
can only ever be orange, so a Ceph outage that stops every session start looks
the same as one stray timeout. The row is the share of CephFS mount attempts on
AF nodes that succeeded, so it reaches red exactly when nothing mounts.
"""

import json
import re

from common import REPO

DEFAULT = REPO / "apps/monitoring/grafana/dashboards/default.json"

FATAL = {"DeadlineExceeded", "Unavailable", "Internal", "ResourceExhausted"}

# (sum(increase(csi_operations_seconds_count{…}[30m]) and on(node) …) or vector(0))
TERM = re.compile(
    r"\(sum\(increase\(csi_operations_seconds_count\{(?P<sel>[^{}]*)\}"
    r"\[(?P<window>[^\]]+)\]\) and on\(node\) group by \(node\)"
    r"\(kube_node_spec_taint\{[^{}]*\}\)\) or vector\(0\)\)"
)

SKELETON = "1 - X / clamp_min(X, 1)"


def _painted(failed, attempted):
    """What SKELETON evaluates to for a failed count out of an attempted one."""
    return 1 - failed / max(attempted, 1)


def _panel(title):
    doc = json.loads(DEFAULT.read_text())
    (panel,) = [p for p in doc["panels"] if p.get("title") == title]
    return panel


def _target(panel, legend):
    (target,) = [t for t in panel["targets"] if t.get("legendFormat") == legend]
    return target


def _statuses(selector):
    (codes,) = re.findall(r'grpc_status_code=~"([^"]*)"', selector)
    return set(codes.split("|"))


def test_geddes_storage_is_a_failed_over_attempted_ratio():
    expr = _target(_panel("Components"), "Geddes storage")["expr"]
    terms = list(TERM.finditer(expr))
    assert len(terms) == 2, expr
    assert TERM.sub("X", expr) == SKELETON, expr

    failed, attempted = (_statuses(t.group("sel")) for t in terms)
    assert failed == FATAL, failed
    assert attempted == FATAL | {"OK"}, attempted
    assert {t.group("window") for t in terms} == {"30m"}

    # Both halves count the same calls on the same nodes, so the ratio stays
    # within [0, 1] and the row cannot read above green or below red.
    selectors = [
        re.sub(r'grpc_status_code=~"[^"]*",?', "", t.group("sel")) for t in terms
    ]
    assert selectors[0] == selectors[1], selectors


def test_geddes_storage_spans_the_full_panel_range():
    steps = _panel("Components")["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert [s["color"] for s in steps] == ["red", "orange", "green"]
    assert [s["value"] for s in steps] == [None, 0.0001, 1]

    assert _painted(0, 0) == 1  # an idle window is not a failing one
    assert _painted(0, 40) == 1  # every mount succeeded
    assert _painted(13, 13) == 0  # every mount failed: an outage, painted red
    assert 0 < _painted(5, 15) < 1  # some failed: partial, painted orange
