"""Tests for the gpu-culler managed service — apps/jupyterhub/jupyterhub.

The cull logic is covered in tests/jupyterhub_config; this checks the wiring.
A managed service gets a fresh environment (JUPYTERHUB_*, PATH, LANG), not the
hub's, so everything the culler needs must be passed explicitly."""

import re

from common import REPO, load_yaml

HUB = REPO / "apps/jupyterhub/jupyterhub"
SCRIPT = HUB / "extraFiles/cull-gpu-sessions.py"
PROMETHEUS = REPO / "apps/monitoring/prometheus/values.yaml"


def hub_values():
    return load_yaml(HUB / "values.yaml")["hub"]


def culler_service():
    return hub_values()["services"]["gpu-culler"]


def test_service_gets_the_apiserver_from_its_own_environment():
    env = culler_service()["environment"]
    assert env["KUBERNETES_SERVICE_HOST"] == "kubernetes.default.svc"
    assert str(env["KUBERNETES_SERVICE_PORT"]) == "443"


def test_output_is_not_swallowed_by_stdout_buffering():
    assert "-u" in culler_service()["command"]


def test_command_points_at_the_mounted_configmap():
    command = culler_service()["command"]
    script = next(a for a in command if a.endswith("cull-gpu-sessions.py"))

    mounts = {m["name"]: m for m in hub_values()["extraVolumeMounts"]}
    volumes = {v["name"]: v for v in hub_values()["extraVolumes"]}
    mount = mounts["gpu-culler-script"]

    assert script == f"{mount['mountPath']}/{SCRIPT.name}"
    assert volumes["gpu-culler-script"]["configMap"]["name"] == "jupyterhub-gpu-culler"


def test_role_grants_the_scopes_the_cull_pass_uses():
    role = hub_values()["loadRoles"]["gpu-culler"]
    assert role["services"] == ["gpu-culler"]
    assert {"read:servers", "delete:servers"} <= set(role["scopes"])


def test_the_alert_on_a_dead_culler_waits_out_one_timeout_and_one_pass():
    arguments = dict(
        a.removeprefix("--").split("=")
        for a in culler_service()["command"]
        if a.startswith("--")
    )
    timeout, every = int(arguments["timeout"]), int(arguments["every"])
    doc = load_yaml(PROMETHEUS)
    groups = doc["serverFiles"]["alerting_rules.yml"]["groups"]
    rule = next(
        r
        for g in groups
        for r in g["rules"]
        if r["alert"] == "AFGpuSessionsPastIdleTimeout"
    )
    hours = int(re.search(r"> (\d+) \* 3600", rule["expr"]).group(1))
    assert timeout + every < hours * 3600 < timeout + 2 * 3600
