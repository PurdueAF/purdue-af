"""The probe DaemonSets and the exporter that reads them must agree.

The exporter names the mounts it publishes; the DaemonSets decide which are
probed. A mount in one and not the other would read "never reported" forever.
"""

import ast
import re

import yaml
from common import REPO

PROBES = REPO / "apps/monitoring/af-monitoring/daemonset-af-node-probe.yaml"
EXPORTER = REPO / "docker/af-node-monitor/node_healthcheck.py"
AGENT = REPO / "docker/af-node-monitor/probe_agent.py"
DEPLOYMENT = REPO / "apps/monitoring/af-monitoring/deployment-af-node-monitor.yaml"
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
    infra = {"scripts", "runtime"}
    for name, ds in daemonsets().items():
        volumes = {v["name"] for v in ds["spec"]["template"]["spec"]["volumes"]}
        assert volumes & infra == infra, name
        assert len(volumes - infra) == 1, name


def test_the_monitored_mount_is_the_only_volume_that_can_fail():
    """A shared network volume in every probe pod makes one storage fault
    blind every mount on every node; the rest stay node-local."""
    for name, ds in daemonsets().items():
        volumes = {v["name"]: v for v in ds["spec"]["template"]["spec"]["volumes"]}
        assert volumes["runtime"]["emptyDir"] == {}, name
        assert volumes["scripts"]["configMap"]["name"] == "af-node-monitor-config", name


def test_probes_and_the_exporter_mount_no_claim_of_their_own():
    monitored = {"work": "af-shared-storage", "cvmfs": "cvmfs"}
    for name, ds in daemonsets().items():
        mount = ds["metadata"]["labels"]["mount"]
        claims = {
            v["persistentVolumeClaim"]["claimName"]
            for v in ds["spec"]["template"]["spec"]["volumes"]
            if "persistentVolumeClaim" in v
        }
        assert claims <= {monitored.get(mount)}, name
    exporter = yaml.safe_load(DEPLOYMENT.read_text())["spec"]["template"]["spec"]
    assert not any("persistentVolumeClaim" in v for v in exporter["volumes"])


# ── the four specs stay identical where they must ─────────────────────────────


def test_scheduling_is_identical_across_probes():
    specs = [ds["spec"]["template"]["spec"] for ds in daemonsets().values()]
    for field in ("affinity", "tolerations", "priorityClassName"):
        values = [s[field] for s in specs]
        assert all(v == values[0] for v in values), field


def test_probes_and_resources_are_identical():
    containers = [container(ds) for ds in daemonsets().values()]
    for field in (
        "resources",
        "livenessProbe",
        "readinessProbe",
        "imagePullPolicy",
        "ports",
    ):
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


def http_port():
    ports = {
        p["name"]: p["containerPort"]
        for p in container(next(iter(daemonsets().values())))["ports"]
    }
    return ports["http"]


def test_the_http_port_is_the_one_the_agent_serves():
    """The exporter finds the port by its name; the agent binds its default."""
    default = re.search(r'_get_env\("PROBE_HTTP_PORT", "(\d+)"\)', AGENT.read_text())
    assert default, "PROBE_HTTP_PORT default not found in probe_agent.py"
    assert http_port() == int(default.group(1))


def probe_cmd(ds, which):
    return " ".join(container(ds)[which]["exec"]["command"])


def agent_default(name):
    found = re.search(rf'_get_env\("{name}", "([^"]+)"\)', AGENT.read_text())
    assert found, f"{name} default not found in probe_agent.py"
    return found.group(1)


def test_liveness_and_readiness_read_the_emptydir():
    """A check that touches the wedged mount hangs instead of failing, and one
    over the network can be held by any client that reaches the pod."""
    runtime = agent_default("PROBE_RUNTIME_DIR")
    for name, ds in daemonsets().items():
        mounts = {m["name"]: m["mountPath"] for m in container(ds)["volumeMounts"]}
        assert mounts["runtime"] == runtime, name
        for which in ("livenessProbe", "readinessProbe"):
            assert f"{runtime}/heartbeat" in probe_cmd(ds, which), (name, which)
        assert f"{runtime}/ready" in probe_cmd(ds, "readinessProbe"), name
        assert "$READY_WINDOW_S" in probe_cmd(ds, "readinessProbe"), name
        assert "$LIVENESS_WINDOW_S" in probe_cmd(ds, "livenessProbe"), name


def test_health_windows_cover_a_full_cycle():
    """A healthy loop sleeps a full interval, plus the first cycle's jitter,
    and an attempt may burn its whole deadline; a tighter window fails a
    working probe."""
    one_cycle = sum(
        float(agent_default(key))
        for key in (
            "PROBE_INTERVAL_S",
            "PROBE_STARTUP_JITTER_S",
            "PROBE_DEADLINE_S",
            "CHILD_KILL_GRACE_S",
        )
    )
    for name, ds in daemonsets().items():
        env = env_of(ds)
        assert float(env["READY_WINDOW_S"]) > one_cycle, name
        assert float(env["LIVENESS_WINDOW_S"]) > float(env["READY_WINDOW_S"]), name


def test_probes_get_no_api_token():
    """The probes never call the API; only the exporter does."""
    for name, ds in daemonsets().items():
        assert ds["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


def test_image_is_not_repulled_on_every_restart():
    """A registry outage must not take mount monitoring down with it."""
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


# ── one dead node must not freeze every other node ────────────────────────────


def test_rollouts_tolerate_a_permanently_unavailable_node():
    """maxUnavailable defaults to 1, and a probe pod that can never become
    available — a node whose CSI driver is missing, so its volume never mounts
    — holds that budget forever. At the default, no other node would ever be
    updated again."""
    for name, ds in daemonsets().items():
        strategy = ds["spec"]["updateStrategy"]
        assert strategy["type"] == "RollingUpdate", name
        budget = strategy["rollingUpdate"]["maxUnavailable"]
        assert budget != 1, name
        assert str(budget).endswith("%"), (name, budget)


# ── the sentinel and the timeout it stands for ────────────────────────────────


def exporter_env():
    doc = yaml.safe_load(DEPLOYMENT.read_text())
    container = doc["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container["env"]}


def test_probes_carry_their_own_timeouts():
    """Every probe sets its own timeouts; an unset one falls back to
    job_runner's default silently."""
    for name, ds in daemonsets().items():
        env = env_of(ds)
        assert env.get("PING_TIMEOUT_S"), name
        assert env.get("METADATA_TIMEOUT_S"), name
        if env["ENABLE_FIO"] == "true":
            assert env.get("FIO_TIMEOUT_S"), name
            assert env.get("FIO_INTERVAL_S"), name


def test_exporter_sentinels_match_the_probe_timeouts():
    """af_node_mount_ping_ms reports PING_TIMEOUT_S when a check gives up. If
    the exporter's copy drifts from the probe's real timeout, that gauge charts
    a latency nothing ever measured — and AFMountSlow's `< 10000` guard, which
    exists to keep timed-out probes out of that alert, stops matching."""
    exporter = exporter_env()
    for key in ("PING_TIMEOUT_S", "METADATA_TIMEOUT_S"):
        assert exporter.get(key), f"exporter does not set {key}"
        for name, ds in daemonsets().items():
            assert env_of(ds)[key] == exporter[key], (name, key)


def test_exporter_does_not_keep_a_timeout_it_cannot_enforce():
    """The exporter runs no fio, so it carries no fio timeout."""
    assert "FIO_TIMEOUT_S" not in EXPORTER.read_text()
