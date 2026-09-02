"""Deployment configuration, read from the environment in one place.

Every URL the service talks to is defined here so that no two modules can
disagree about where a backend lives; apps/agentic-interface/deployment.yaml
sets the environment.
"""

import os

# JupyterHub registers this service under a prefix; the MCP endpoint is
# ``{SERVICE_PREFIX}/mcp``.
SERVICE_PREFIX = os.environ.get(
    "JUPYTERHUB_SERVICE_PREFIX", "/services/agentic-interface"
).rstrip("/")
NAMESPACE = os.environ.get("NAMESPACE", "cms")

HUB_API_URL = os.environ.get("JUPYTERHUB_API_URL", "http://hub:8081/hub/api")
# Public base URL of the facility, for user-facing links.
PUBLIC_URL = os.environ.get(
    "AF_PUBLIC_URL", "https://cms.geddes.rcac.purdue.edu"
).rstrip("/")
# Where a user mints an API token — every authentication failure points here.
TOKEN_URL = f"{PUBLIC_URL}/hub/token"

# The AF Prometheus (dask-gateway-monitor, af-pod-monitor, alert rules) …
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-server:9090")
# … and the cluster (Rancher) Prometheus, which is where cadvisor / kubelet
# container_* metrics live.
CLUSTER_PROMETHEUS_URL = os.environ.get(
    "CLUSTER_PROMETHEUS_URL",
    "http://rancher-monitoring-prometheus.cattle-monitoring-system.svc.cluster.local:9090",
)
LOKI_URL = os.environ.get("LOKI_URL", "http://loki.cms.svc.cluster.local:3100")

# Dask Gateway backends: name → internal API URL.
DASK_GATEWAYS: dict[str, str] = {
    "k8s": os.environ.get(
        "DASK_GATEWAY_K8S_URL",
        "http://api-dask-gateway-k8s.cms.svc.cluster.local:8000",
    ),
    "slurm": os.environ.get(
        "DASK_GATEWAY_SLURM_URL",
        "http://api-dask-gateway-k8s-slurm.cms.svc.cluster.local:8000",
    ),
}
# Shared pixi env pre-built for everyone. Lives on /work, so it is only usable
# by Kubernetes workers — Slurm (Hammer) workers can see /depot but not /work.
GLOBAL_PIXI_PROJECT = os.environ.get("DASK_GLOBAL_PIXI_PROJECT", "/work/pixi/global")

# Stateful streamable-HTTP sessions are required for server→client requests
# such as elicitation. Stateless mode keeps one-shot POSTs working (handy for
# curl) but disables elicitation; the deployment (single replica) sets false.
STATELESS_HTTP = os.environ.get("MCP_STATELESS_HTTP", "true").lower() in (
    "1",
    "true",
    "yes",
)
