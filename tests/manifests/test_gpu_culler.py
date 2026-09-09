"""Tests for the gpu-culler managed service — apps/jupyterhub/jupyterhub.

The cull logic itself is covered in tests/jupyterhub_config; what is checked
here is the wiring around it, which is where it actually broke. JupyterHub
does not hand a managed service the hub's environment — it builds a fresh one
holding JUPYTERHUB_*, PATH and LANG — so the culler ran for two months with no
KUBERNETES_SERVICE_HOST, raising `ConfigException: Service host/port is not
set.` on every pass and culling nothing."""

import re

import yaml
from common import REPO

HUB = REPO / "apps/jupyterhub/jupyterhub"
SCRIPT = HUB / "extraFiles/cull-gpu-sessions.py"


def hub_values():
    return yaml.safe_load((HUB / "values.yaml").read_text())["hub"]


def culler_service():
    return hub_values()["services"]["gpu-culler"]


def test_service_gets_the_apiserver_from_its_own_environment():
    env = culler_service()["environment"]
    assert env["KUBERNETES_SERVICE_HOST"] == "kubernetes.default.svc"
    assert str(env["KUBERNETES_SERVICE_PORT"]) == "443"


def test_output_is_not_swallowed_by_stdout_buffering():
    assert "-u" in culler_service()["command"]


def test_namespace_comes_from_the_service_account_not_a_hardcoded_default():
    """POD_NAMESPACE is stripped along with the rest of the hub env, so the
    script must not rely on it being present."""
    source = SCRIPT.read_text()
    assert "/var/run/secrets/kubernetes.io/serviceaccount/namespace" in source
    assert "--namespace" in source
    # no `default="cms"` style fallback that would silently list the wrong ns
    assert not re.search(r'POD_NAMESPACE["\'],\s*["\']\w', source)


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
