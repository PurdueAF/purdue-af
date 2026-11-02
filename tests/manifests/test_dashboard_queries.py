"""What dashboard queries rely on outside their own text: the Prometheus that
holds a metric, the variables they name, the worker shape they divide by."""

import json
import re

import yaml
from common import REPO, load_script
from dashboard_helpers import DASHBOARDS, dashboard, panel

# Rancher's Prometheus alone holds these; `\b` leaves kube_pod_container_* out.
RANCHER_ONLY = re.compile(r"\b(container|kubelet_volume)_")
# Also Grafana's default, so a panel or target that names no datasource reads it.
AF_PROMETHEUS = "prometheus"
MIXED = "-- Mixed --"
NRP_TEMPLATE = REPO / "apps/ray-train/rayjob-nrp.yaml"


def _uid(owner):
    return (owner.get("datasource") or {}).get("uid", AF_PROMETHEUS)


def on_the_af_prometheus(doc):
    """(panel, refId) of each Rancher-only query that resolves to the AF Prometheus."""
    panels = doc["panels"] + [p for row in doc["panels"] for p in row.get("panels", [])]
    return [
        (p.get("title"), t.get("refId"))
        for p in panels
        for t in p.get("targets", [])
        if RANCHER_ONLY.search(t.get("expr", ""))
        and (_uid(t) if _uid(p) == MIXED else _uid(p)) == AF_PROMETHEUS
    ]


def test_cadvisor_and_kubelet_queries_reach_rancher():
    names = sorted(path.name for path in DASHBOARDS.glob("*.json"))
    assert names
    for name in names:
        assert not on_the_af_prometheus(dashboard(name)), name


def test_gpus_table_divides_by_the_nrp_worker():
    """Each query repeats the divisors, so a size edited in one place fails too."""
    nrp = load_script(REPO / "apps/ray-train/nrp_gpus.py", "nrp_gpus_dashboards")
    worker = nrp.worker_of(yaml.safe_load(NRP_TEMPLATE.read_text()))
    divisors = {f"{worker.cpus:g}", f"{worker.memory_gb * 2**30:.0f}"}
    targets = {t["refId"]: t["expr"] for t in panel("default.json", "GPUs")["targets"]}
    for ref in ("NA", "NC", "ND"):
        assert set(re.findall(r"/ ([\d.]+)", targets[ref])) == divisors, ref


def test_raw_variables_are_defined():
    """`$$` is Flux's escape: the panel gets `${name:raw}` and Grafana expands it."""
    doc = dashboard("default.json")
    used = set(re.findall(r"\$\$\{(?!__)(\w+)", json.dumps(doc)))
    assert used
    assert used <= {v["name"] for v in doc["templating"]["list"]}


def test_tool_groups_are_constants_and_ungrouped_negates_every_group():
    doc = dashboard("agentic-interface.json")
    variables = doc["templating"]["list"]
    constants = {v["name"] for v in variables if v["type"] == "constant"}
    used = set(re.findall(r"\$(\w+_tools)\b", json.dumps(doc)))
    assert used
    assert used <= constants
    groups = {name for name in constants if name.endswith("_tools")}
    area = panel("agentic-interface.json", "Activity by area")
    (ungrouped,) = [t for t in area["targets"] if t["legendFormat"] == "Ungrouped"]
    (negated,) = re.findall(r'tool!~"([^"]*)"', ungrouped["expr"])
    assert set(negated.split("|")) == {
        f"${name}" for name in groups - {"facility_change_tools"}
    }
