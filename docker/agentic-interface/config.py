"""Deployment configuration: where each backend lives, in one place.

The defaults are the facility's own addresses; the environment overrides them.
"""

import os

# The MCP endpoint is ``{SERVICE_PREFIX}/mcp``.
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

# The AF Prometheus (dask-gateway-monitor, af-pod-monitor, alert rules), and …
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-server:9090")
PROMETHEUS_NAME = "Prometheus"
# … the cluster (Rancher) Prometheus, with container_*; messages name them apart.
CLUSTER_PROMETHEUS_URL = os.environ.get(
    "CLUSTER_PROMETHEUS_URL",
    "http://rancher-monitoring-prometheus.cattle-monitoring-system.svc.cluster.local:9090",
)
CLUSTER_PROMETHEUS_NAME = "the monitoring system"
LOKI_URL = os.environ.get("LOKI_URL", "http://loki.cms.svc.cluster.local:3100")

DASK_GATEWAY_URL = os.environ.get(
    "DASK_GATEWAY_URL", "http://api-dask-gateway.cms.svc.cluster.local:8000"
)
RAY_GATEWAY_URL = os.environ.get(
    "RAY_GATEWAY_URL", "http://ray-train-gateway.cms.svc.cluster.local:8265"
)
# Shared pixi env pre-built for everyone.
GLOBAL_PIXI_PROJECT = os.environ.get("DASK_GLOBAL_PIXI_PROJECT", "/work/pixi/global")

# The public dashboard, from the grafana-public-dashboards ConfigMap.
DASHBOARD_PATH = os.environ.get("DASHBOARD_PATH", "/dashboards/default.json")

# The agent skill, bundled so the server can announce its current version.
SKILL_PATH = os.environ.get("SKILL_PATH", "/app/SKILL.md")

# Elicitation needs stateful sessions; stateless keeps one-shot POSTs working.
STATELESS_HTTP = os.environ.get("MCP_STATELESS_HTTP", "true").lower() in (
    "1",
    "true",
    "yes",
)
