"""Tests for the AF Prometheus scrape of /hub/metrics — apps/jupyterhub/jupyterhub
and apps/monitoring/prometheus.

/hub/metrics answers only a token with read:metrics. z2jh generates each hub
service's token and keeps it in the `hub` Secret as hub.services.<name>.apiToken."""

from common import EXTRA_FILES, REPO, hub_values, load_yaml

PROMETHEUS = REPO / "apps/monitoring/prometheus/values.yaml"


def prometheus_values():
    return load_yaml(PROMETHEUS)


def hub_job():
    jobs = prometheus_values()["serverFiles"]["prometheus.yml"]["scrape_configs"]
    return next(j for j in jobs if j["job_name"] == "jupyterhub")


def metrics_service():
    """The one hub service whose role grants read:metrics."""
    (name,) = {
        service
        for role in hub_values()["hub"]["loadRoles"].values()
        if "read:metrics" in role["scopes"]
        for service in role.get("services", [])
    }
    assert name in hub_values()["hub"]["services"]
    return name


def test_the_hub_does_not_turn_metrics_auth_off():
    assert "authenticate_prometheus" not in hub_values()["hub"]["config"].get(
        "JupyterHub", {}
    )
    for snippet in EXTRA_FILES.glob("*.py"):
        assert "authenticate_prometheus" not in snippet.read_text(), snippet.name


def test_the_scrape_sends_the_metrics_service_token():
    server = prometheus_values()["server"]
    path = hub_job()["authorization"]["credentials_file"]
    mount = next(
        m for m in server["extraVolumeMounts"] if path.startswith(m["mountPath"] + "/")
    )
    volume = next(v for v in server["extraVolumes"] if v["name"] == mount["name"])
    secret = volume["secret"]
    assert secret["secretName"] == "hub"
    (item,) = secret["items"]
    assert item["key"] == f"hub.services.{metrics_service()}.apiToken"
    assert path == f"{mount['mountPath']}/{item['path']}"


def test_the_token_is_only_sent_over_tls():
    assert hub_job()["scheme"] == "https"
