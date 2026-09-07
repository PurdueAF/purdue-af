"""Contracts for the Triton-on-Ray dashboard.

A Grafana dashboard fails silently: a renamed metric, a label that does not
exist or a missing release filter renders an empty panel, or worse, one that
quietly sums two releases together. These hold the parts that were checked
against the sources rather than guessed:

  * Ray Serve metric names come from Ray 2.52's own definitions
    (python/ray/serve/_private/{replica,router,proxy,deployment_state}.py).
  * `method` is a declared tag key of the proxy's request counter; `pod`,
    `release` and `namespace` are attached by the AF Prometheus itself
    (af-pod-monitor relabel_configs in apps/monitoring/prometheus/values.yaml),
    so panels group by those rather than by Ray's internal replica tags.
  * The nv_* series and the datasource uid match what the SuperSONIC
    dashboard already plots on this Grafana.
"""

import json
import re

import yaml
from common import REPO

DASHBOARD = REPO / "apps/monitoring/grafana/dashboards/sonic-ray.json"
DASHBOARD_FILE = "sonic-ray.json"
KUSTOMIZATION = REPO / "deploy/core-production/kustomization.yaml"
PUBLIC_VALUES = REPO / "apps/monitoring/grafana/values.yaml"
PRIVATE_VALUES = REPO / "apps/monitoring/grafana/values-private.yaml"

# Every metric the dashboard is allowed to name, and where the name was
# verified. Adding one means checking it the same way.
RAY_METRICS = {
    "ray_serve_deployment_replica_healthy",  # deployment_state.py
    "ray_serve_deployment_processing_latency_ms",  # replica.py (histogram)
    "ray_serve_replica_processing_queries",  # replica.py
    "ray_serve_deployment_error_counter_total",  # replica.py
    "ray_serve_deployment_queued_queries",  # router.py
    "ray_serve_num_grpc_requests_total",  # proxy.py: serve_num_{protocol}_requests
    "ray_serve_num_grpc_error_requests_total",  # proxy.py
}
TRITON_METRICS = {
    "nv_inference_count",
    "nv_inference_exec_count",
    "nv_inference_request_duration_us",
    "nv_inference_request_success",
    "nv_inference_queue_duration_us",
    "nv_inference_compute_input_duration_us",
    "nv_inference_compute_infer_duration_us",
    "nv_inference_compute_output_duration_us",
    "nv_inference_pending_request_count",
    "nv_gpu_utilization",
    "nv_gpu_memory_used_bytes",
    "nv_gpu_memory_total_bytes",
}
# Labels a panel may group by: declared Ray tag keys, or labels the AF
# Prometheus attaches during relabeling.
GROUPABLE = {"method", "model", "pod", "le"}

METRIC = re.compile(r"\b(ray_serve_[a-z_]+|nv_[a-z_]+)\b")
BY = re.compile(r"\bby\s*\(([^)]*)\)")


def dashboard():
    return json.loads(DASHBOARD.read_text())


def panels():
    return [p for p in dashboard()["panels"] if p["type"] not in ("row", "text")]


def exprs():
    return [
        (p["title"], t["expr"])
        for p in panels()
        for t in p.get("targets", [])
        if t.get("expr")
    ]


def generated_dashboards():
    """{generator name: [dashboard filenames]} from the deploy kustomization."""
    doc = yaml.safe_load(KUSTOMIZATION.read_text())
    return {
        cm["name"]: [f.rsplit("/", 1)[-1] for f in cm.get("files", [])]
        for cm in doc["configMapGenerator"]
        if cm["name"].startswith("grafana-")
    }


def test_dashboard_is_provisioned_to_the_public_instance():
    """Grafana is two instances since the public/private split: the public one
    is anonymous-viewer readable and carries the SuperSONIC dashboard, so its
    sibling belongs there too. A dashboard in neither ConfigMap is a file in
    git and nothing else; one in the private ConfigMap would be invisible to
    the people watching a SONIC benchmark."""
    generated = generated_dashboards()
    assert DASHBOARD_FILE in generated["grafana-public-dashboards"]
    assert DASHBOARD_FILE not in generated["grafana-private-dashboards"]
    # ...and it sits with the release it is the counterpart of.
    assert "sonic.json" in generated["grafana-public-dashboards"]


def test_the_public_configmap_is_the_one_the_public_instance_mounts():
    """The ConfigMap name above is only meaningful if that instance mounts it;
    the split gave each instance its own map and its own provider."""
    public = yaml.safe_load(PUBLIC_VALUES.read_text())["dashboardsConfigMaps"]
    private = yaml.safe_load(PRIVATE_VALUES.read_text())["dashboardsConfigMaps"]
    assert public["public"] == "grafana-public-dashboards"
    assert private["private"] == "grafana-private-dashboards"
    assert "grafana-public-dashboards" not in private.values()


def test_identity_is_stable():
    """The uid is what links and bookmarks resolve; renaming it orphans them."""
    doc = dashboard()
    assert doc["uid"] == "sonic-ray"
    assert doc["title"] == "Triton on Ray"


def test_every_series_is_one_the_exporters_actually_publish(subtests=None):
    """Guards against plausible-looking names that no exporter emits."""
    known = RAY_METRICS | TRITON_METRICS
    for title, expr in exprs():
        for metric in METRIC.findall(expr):
            base = re.sub(r"_(bucket|sum|count)$", "", metric)
            assert metric in known or base in known, f"{title}: unknown series {metric}"


def test_every_panel_is_scoped_to_one_release():
    """Without both filters a panel sums this release with any other Ray or
    Triton release in the cluster — silently, and plausibly."""
    for title, expr in exprs():
        assert 'release=~"$release"' in expr, title
        assert 'namespace=~"$namespace"' in expr, title


def test_panels_group_only_by_labels_that_exist():
    """Ray's internal tags (deployment, replica, application) are not what the
    AF Prometheus keys on; pod/release/namespace are added by its relabeling,
    and method/model are declared tag keys of the exporters."""
    for title, expr in exprs():
        for clause in BY.findall(expr):
            for label in (label.strip() for label in clause.split(",")):
                assert label in GROUPABLE, f"{title}: groups by {label!r}"


def test_rate_windows_follow_the_dashboard_interval():
    """A hardcoded window under-samples when someone zooms out to a week."""
    for title, expr in exprs():
        if "rate(" in expr:
            assert "[$__rate_interval]" in expr, title


def test_divisions_cannot_produce_infinity():
    """An idle release divides by a zero rate; clamp_min keeps the panel from
    rendering ±∞ (the failure the pixi panels already learned)."""
    for title, expr in exprs():
        for denominator in expr.split(" / ")[1:]:
            denominator = denominator.lstrip()
            if re.match(r"^[0-9.]+\b", denominator):
                continue  # a constant (unit conversion) is never zero
            assert denominator.startswith("clamp_min("), (
                f"{title}: unguarded division by {denominator.strip()[:60]}"
            )


def test_overhead_panel_compares_like_with_like():
    """The point of the dashboard: Serve's latency minus Triton's own. Serve
    reports milliseconds, Triton microseconds — the conversion is what makes
    the subtraction mean anything."""
    (panel,) = [p for p in panels() if p["title"] == "Ray overhead"]
    (expr,) = [t["expr"] for t in panel["targets"]]
    serve, _, triton = expr.partition(" - ")
    assert "ray_serve_deployment_processing_latency_ms_sum" in serve
    assert "nv_inference_request_duration_us" in triton
    assert triton.rstrip().endswith("/ 1000"), "µs are not ms"
    assert panel["fieldConfig"]["defaults"]["unit"] == "ms"
    # Overhead is the only panel that may legitimately go negative (clock
    # skew, different windows); it must not be clipped at zero.
    assert "min" not in panel["fieldConfig"]["defaults"]


def test_autoscaling_panel_shows_the_signal_and_the_response():
    """Replicas alone do not explain a scaling decision; the in-flight average
    is what Serve acts on and queued requests are what it failed to absorb."""
    (panel,) = [p for p in panels() if p["title"] == "Autoscaling"]
    legends = {t["legendFormat"] for t in panel["targets"]}
    assert {"replicas", "in flight", "queued"} == legends


def test_every_querying_panel_uses_the_af_datasource():
    """The AF Grafana provisions exactly one Prometheus, uid 'prometheus'. Rows
    carry no datasource of their own, which is correct."""
    expected = {"type": "prometheus", "uid": "prometheus"}
    for panel in panels():
        assert panel["datasource"] == expected, panel["title"]
        for target in panel.get("targets", []):
            assert target["datasource"] == expected, panel["title"]
    assert all(
        "datasource" not in p for p in dashboard()["panels"] if p["type"] == "row"
    )


def test_panels_do_not_overlap():
    """Two panels on the same square is a rendering bug that only shows in the
    browser, never in review."""
    taken = {}
    for panel in dashboard()["panels"]:
        pos = panel["gridPos"]
        for x in range(pos["x"], pos["x"] + pos["w"]):
            for y in range(pos["y"], pos["y"] + pos["h"]):
                assert (x, y) not in taken, (
                    f"{panel['title']!r} overlaps {taken[(x, y)]!r} at {(x, y)}"
                )
                taken[(x, y)] = panel["title"]
        assert pos["x"] + pos["w"] <= 24, panel["title"]
