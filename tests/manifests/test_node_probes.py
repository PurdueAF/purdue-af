"""The probe DaemonSets and the exporter that reads them must agree.

Splitting the probe spec out of node_healthcheck.py removed one source of
drift (the Job template) and created another: the exporter still names the
mounts it publishes, while the DaemonSets decide which mounts are actually
probed. A mount in one and not the other reports "never reported" forever,
which is the failure this suite exists to catch — nothing else notices, because
a mount nobody probes looks exactly like a mount nobody can reach.
"""

import ast

import yaml
from common import REPO

PROBES = REPO / "apps/monitoring/af-monitoring/daemonset-af-node-probe.yaml"
EXPORTER = REPO / "docker/af-node-monitor/node_healthcheck.py"
DEPLOYMENTS = (
    REPO / "deploy/experimental/kustomization.yaml",
    REPO / "deploy/core-geddes2/kustomization.yaml",
)


def docs():
    return [d for d in yaml.safe_load_all(PROBES.read_text()) if d]


def daemonsets():
    return {d["metadata"]["name"]: d for d in docs() if d["kind"] == "DaemonSet"}


def container(ds):
    return ds["spec"]["template"]["spec"]["containers"][0]


def env_of(ds):
    return {e["name"]: e.get("value") for e in container(ds)["env"]}


def exporter_mounts():
    """node_healthcheck.MOUNTS, read without importing (no k8s client here)."""
    tree = ast.parse(EXPORTER.read_text())
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        if getattr(node.target, "id", None) == "MOUNTS":
            return ast.literal_eval(node.value)
    raise AssertionError("MOUNTS not found in node_healthcheck.py")


# ── the two halves describe the same set of mounts ────────────────────────────


def test_every_exported_mount_has_a_probe():
    probed = {env_of(ds)["MOUNT_NAME"] for ds in daemonsets().values()}
    assert probed == set(exporter_mounts())


def test_mount_labels_match_the_exporter_key():
    """The exporter joins pods to results on the sanitised mount name; the
    label has to be that exact string or every probe looks absent."""
    for ds in daemonsets().values():
        mount_name = env_of(ds)["MOUNT_NAME"]
        expected = mount_name.strip("/").replace("/", "_") or "root"
        assert ds["metadata"]["labels"]["mount"] == expected, ds["metadata"]["name"]


def test_exporter_selector_matches_the_probe_labels():
    selector = "app=af-node-monitor,component=probe"
    assert selector in EXPORTER.read_text()
    for ds in daemonsets().values():
        labels = ds["spec"]["template"]["metadata"]["labels"]
        for term in selector.split(","):
            key, value = term.split("=")
            assert labels.get(key) == value, ds["metadata"]["name"]


def test_selector_matches_pod_labels():
    for name, ds in daemonsets().items():
        assert (
            ds["spec"]["selector"]["matchLabels"]
            == ds["spec"]["template"]["metadata"]["labels"]
        ), name


# ── per-mount isolation ───────────────────────────────────────────────────────


def test_each_daemonset_carries_exactly_one_monitored_mount():
    """One pod holding all four volumes goes to ContainerCreating when any one
    of them will not mount, taking three healthy mounts to unknown with it."""
    infra = {"scripts", "runtime", "results"}
    for name, ds in daemonsets().items():
        volumes = {v["name"] for v in ds["spec"]["template"]["spec"]["volumes"]}
        assert volumes & infra == infra, name
        assert len(volumes - infra) == 1, name


def test_results_volume_is_the_shared_claim_every_probe_writes_to():
    for name, ds in daemonsets().items():
        volumes = {v["name"]: v for v in ds["spec"]["template"]["spec"]["volumes"]}
        claim = volumes["results"]["persistentVolumeClaim"]["claimName"]
        assert claim == "af-node-monitor-storage", name
        assert volumes["runtime"]["emptyDir"] == {}, name
        assert volumes["scripts"]["configMap"]["name"] == "af-node-monitor-config", name


# ── the four specs stay identical where they must ─────────────────────────────


def test_scheduling_is_identical_across_probes():
    specs = [ds["spec"]["template"]["spec"] for ds in daemonsets().values()]
    for field in ("affinity", "tolerations", "priorityClassName"):
        values = [s[field] for s in specs]
        assert all(v == values[0] for v in values), field


def test_probes_and_resources_are_identical():
    containers = [container(ds) for ds in daemonsets().values()]
    for field in ("resources", "livenessProbe", "readinessProbe", "imagePullPolicy"):
        values = [c[field] for c in containers]
        assert all(v == values[0] for v in values), field


def test_probe_targets_only_af_nodes():
    terms = [
        term
        for ds in daemonsets().values()
        for term in ds["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"]
    ]
    keys = {expr["key"] for term in terms for expr in term["matchExpressions"]}
    assert keys == {"cms-af-prod", "cms-af-dev"}


def test_probes_tolerate_the_cms_af_taint():
    """paf-* nodes carry it; without the toleration the probe never lands and
    every mount on them reads as unknown."""
    for name, ds in daemonsets().items():
        tolerations = ds["spec"]["template"]["spec"]["tolerations"]
        assert any(t["value"] == "cms-af" for t in tolerations), name


# ── the failure modes the container spec is responsible for ───────────────────


def test_liveness_reads_the_emptydir_heartbeat_not_the_results_pvc():
    """A liveness probe that touches the wedged filesystem hangs instead of
    failing, and kubelet never restarts the container it exists to catch."""
    for name, ds in daemonsets().items():
        cmd = " ".join(container(ds)["livenessProbe"]["exec"]["command"])
        assert "/run/af-node-monitor/heartbeat" in cmd, name
        assert "/af-node-monitor/results" not in cmd, name
        assert container(ds)["livenessProbe"]["timeoutSeconds"] > 0, name


def test_liveness_allows_more_than_one_probe_interval():
    """PROBE_INTERVAL_S is 600s and one attempt may burn 180s of it; a tighter
    window would restart healthy probes mid-check."""
    for name, ds in daemonsets().items():
        cmd = " ".join(container(ds)["livenessProbe"]["exec"]["command"])
        threshold = int(cmd.rsplit("-lt", 1)[1].strip())
        assert threshold >= 600 + 180, name


def test_readiness_tracks_the_probe_not_the_mount():
    for name, ds in daemonsets().items():
        cmd = " ".join(container(ds)["readinessProbe"]["exec"]["command"])
        assert "/run/af-node-monitor/healthy" in cmd, name


def test_image_is_not_repulled_on_every_restart():
    """Every Job used to pull :latest, so a registry outage took mount
    monitoring down with it within one interval."""
    for name, ds in daemonsets().items():
        assert container(ds)["imagePullPolicy"] == "IfNotPresent", name


def test_priority_class_never_preempts():
    """Probes queue ahead of user pods; evicting somebody's Dask worker to
    measure a mount is worse than the gap in coverage."""
    pc = next(d for d in docs() if d["kind"] == "PriorityClass")
    assert pc["preemptionPolicy"] == "Never"
    assert pc["globalDefault"] is False
    assert pc["value"] > 0
    for name, ds in daemonsets().items():
        assert (
            ds["spec"]["template"]["spec"]["priorityClassName"]
            == pc["metadata"]["name"]
        ), name


def test_node_name_comes_from_the_downward_api():
    """One template covers every node, so the result filename and the node
    label can only come from spec.nodeName at runtime."""
    for name, ds in daemonsets().items():
        node_env = next(e for e in container(ds)["env"] if e["name"] == "NODE_NAME")
        assert node_env["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName", name


def test_fio_is_configured_wherever_it_is_enabled():
    for name, ds in daemonsets().items():
        env = env_of(ds)
        if env["ENABLE_FIO"] == "true":
            assert env.get("FIO_FILE"), name
        assert env.get("CHECK_FILE"), name
        assert env.get("METADATA_DIR"), name


# ── deployment wiring ─────────────────────────────────────────────────────────


def test_both_overlays_deploy_the_probes():
    for path in DEPLOYMENTS:
        resources = yaml.safe_load(path.read_text())["resources"]
        assert any("daemonset-af-node-probe.yaml" in r for r in resources), path


def test_both_overlays_ship_every_script_the_probes_run():
    """probe_agent execs job_runner out of the same ConfigMap; shipping only
    the exporter leaves the DaemonSets crashlooping on a missing file."""
    for path in DEPLOYMENTS:
        generators = yaml.safe_load(path.read_text())["configMapGenerator"]
        config = next(g for g in generators if g["name"] == "af-node-monitor-config")
        files = " ".join(config["files"])
        for script in ("node_healthcheck.py", "probe_agent.py", "job_runner.py"):
            assert script in files, (path, script)
