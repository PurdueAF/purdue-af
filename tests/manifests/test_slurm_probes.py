"""Tests for apps/af-utils/slurm-probes — predicted Slurm wait per cluster.

The probe exists because a partition can look idle and still be days deep:
Gautschi was measured at 10,246 idle CPUs and a 3.4-day predicted start under
the standby QoS. So the number only means anything if the probe submits with
the *same* account/partition/QoS a user's Dask cluster would use — that
correspondence is what these tests hold."""

import json
import re
import sys

import yaml
from common import REPO

APP = REPO / "apps/af-utils/slurm-probes"
GATEWAY = REPO / "apps/dask-gateway/dask-gateway-k8s-interlink/values.yaml"
sys.path.insert(0, str(APP))


def deployment():
    return yaml.safe_load((APP / "deployment.yaml").read_text())


def containers():
    return {
        c["name"]: c for c in deployment()["spec"]["template"]["spec"]["containers"]
    }


def gateway_clusters():
    src = yaml.safe_load(GATEWAY.read_text())["gateway"]["extraConfig"]["config"]
    start = src.index("INTERLINK_CLUSTERS = {")
    ns: dict = {}
    exec(src[start : src.index("\n}", start) + 2], ns)
    return ns["INTERLINK_CLUSTERS"]


def _env(container):
    return {e["name"]: e["value"] for e in container.get("env", [])}


def test_one_probe_per_interlink_cluster():
    probes = {n for n in containers() if n.startswith("probe-")}
    assert probes == {f"probe-{c}" for c in gateway_clusters()}


def test_probe_flags_match_what_the_gateway_submits():
    """If these drift, the reported wait is for a job nobody runs."""
    for cluster, meta in gateway_clusters().items():
        env = _env(containers()[f"probe-{cluster}"])
        assert env["PROBE_SBATCH_FLAGS"] == meta["sbatch_flags"], cluster
        assert env["SLURM_CLUSTER"] == cluster


def test_selector_labels_are_parsed_from_the_flags():
    """account/partition/qos are the whole eligibility selector in Slurm — there
    is no separate "queue". Parsing them from the submitted flags rather than
    configuring them twice means the labels cannot describe a different job
    than the one measured."""
    probe = (APP / "probe.sh").read_text()
    for field in ("account", "partition", "qos"):
        assert f"flag_value {field}" in probe, field
        assert f'{field}=\\"${{{field.upper()}}}\\"' in probe, field
    for stale in ("PROBE_PARTITION", "PROBE_QOS"):
        assert stale not in (APP / "deployment.yaml").read_text(), stale


def test_probe_submits_as_the_configured_uid():
    for cluster in gateway_clusters():
        assert _env(containers()[f"probe-{cluster}"])["PROBE_UID"] == "616617"


def test_exports_exactly_one_metric():
    """Deliberately one number; extra series were dropped as noise."""
    probe = (APP / "probe.sh").read_text()
    names = {
        line.split("{")[0].strip()
        for line in probe.splitlines()
        if line.strip().startswith("af_slurm")
    }
    assert names == {"af_slurm_backlog_seconds"}, names


def test_a_failed_probe_emits_no_series():
    """Verified live against Negishi: an unknown wait must not read as zero."""
    probe = (APP / "probe.sh").read_text()
    body = probe[probe.index('if [ "$rc" -ne 0 ]') : probe.index("start=$(date")]
    assert "return" in body
    assert "af_slurm" not in body


def test_each_probe_mounts_its_own_munge_key():
    """Sharing a key across clusters authenticates against none of them."""
    for cluster in gateway_clusters():
        container = containers()[f"probe-{cluster}"]
        claims = {m["name"] for m in container["volumeMounts"]}
        assert f"munge-{cluster}" in claims, cluster
    volumes = {
        v["name"]: v.get("persistentVolumeClaim", {}).get("claimName")
        for v in deployment()["spec"]["template"]["spec"]["volumes"]
    }
    for cluster in gateway_clusters():
        assert volumes[f"munge-{cluster}"] == f"munge-key-{cluster}"


def test_probe_runs_startup_so_it_has_slurm_config_and_munged():
    for cluster in gateway_clusters():
        cmd = " ".join(containers()[f"probe-{cluster}"]["command"])
        assert "/etc/startup.sh" in cmd, cluster


def test_probe_submits_as_a_user_not_root():
    """Testing as root reads root's associations and reports a false
    'Invalid account' — the plugin submits with --uid, so the probe must."""
    probe = (APP / "probe.sh").read_text()
    assert "--uid=" in probe
    assert "PROBE_UID" in probe


def test_probe_never_submits_real_work():
    probe = (APP / "probe.sh").read_text()
    assert "--test-only" in probe
    for line in probe.splitlines():
        if "sbatch" in line and not line.strip().startswith("#"):
            assert "--test-only" in line, line


# --- the exporter ---------------------------------------------------------


def _serve(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_OUT_DIR", str(tmp_path))
    for mod in [m for m in list(sys.modules) if m == "serve"]:
        del sys.modules[mod]
    import serve

    serve.METRICS_DIR = tmp_path
    return serve


def test_exporter_deduplicates_help_and_type(tmp_path, monkeypatch):
    serve = _serve(tmp_path, monkeypatch)
    for cluster in ("a", "b"):
        (tmp_path / f"{cluster}.prom").write_text(
            "# HELP af_slurm_backlog_seconds Predicted wait.\n"
            "# TYPE af_slurm_backlog_seconds gauge\n"
            f'af_slurm_backlog_seconds{{cluster="{cluster}"}} 1\n'
        )
    out = serve.collect()
    assert out.count("# HELP af_slurm_backlog_seconds") == 1
    assert out.count("# TYPE af_slurm_backlog_seconds") == 1
    assert 'cluster="a"' in out and 'cluster="b"' in out


def test_exporter_omits_stale_files(tmp_path, monkeypatch):
    """A probe that stopped reporting must vanish, not read as zero backlog."""
    serve = _serve(tmp_path, monkeypatch)
    fresh = tmp_path / "fresh.prom"
    fresh.write_text('af_slurm_backlog_seconds{cluster="fresh"} 5\n')
    stale = tmp_path / "stale.prom"
    stale.write_text('af_slurm_backlog_seconds{cluster="stale"} 5\n')
    import os

    old = 1
    os.utime(stale, (old, old))
    out = serve.collect()
    assert "fresh" in out
    assert "stale" not in out


def test_exporter_survives_an_empty_directory(tmp_path, monkeypatch):
    serve = _serve(tmp_path, monkeypatch)
    assert serve.collect() == ""


# --- Grafana panel ---------------------------------------------------------

DASHBOARD = REPO / "apps/monitoring/grafana/dashboards/default.json"


def _panels():
    return {p["title"]: p for p in json.loads(DASHBOARD.read_text())["panels"]}


def _backlog_panel():
    return next(p for t, p in _panels().items() if t.startswith("Slurm backlog"))


def test_panel_queries_the_metric_the_probe_actually_emits():
    """A renamed metric that only lands in probe.sh leaves a panel that draws
    an empty graph forever — indistinguishable from a healthy queue."""
    emitted = re.findall(r"^\s*(af_\w+)\{", (APP / "probe.sh").read_text(), re.M)
    exprs = " ".join(t["expr"] for t in _backlog_panel()["targets"])
    assert emitted, "probe.sh emits no metric"
    for metric in set(emitted):
        assert metric in exprs, metric


def test_panel_does_not_stack_or_bridge_gaps():
    """Waits on different clusters are independent, so stacking would invent a
    total nobody waits. And serve.py drops a stale probe rather than reporting
    zero — spanNulls would paper that gap back over."""
    custom = _backlog_panel()["fieldConfig"]["defaults"]["custom"]
    assert custom["stacking"]["mode"] == "none"
    assert custom["spanNulls"] is False


def test_panel_is_in_the_slurm_row_and_the_row_still_fits():
    """Grafana silently reflows a row wider than 24 columns onto a second line."""
    row = [
        p
        for p in json.loads(DASHBOARD.read_text())["panels"]
        if p["gridPos"]["y"] == _backlog_panel()["gridPos"]["y"]
    ]
    assert _backlog_panel() in row
    assert sum(p["gridPos"]["w"] for p in row) == 24


def test_panel_renders_seconds_as_a_duration():
    """The values run to hundreds of thousands; unitless they read as noise."""
    assert _backlog_panel()["fieldConfig"]["defaults"]["unit"] == "s"
