"""Direct Kubernetes API access: quantities, pod discovery, inference endpoint."""

import httpx
import pytest
import respx
from model_manager import kube
from model_manager.config import settings

API = "https://10.0.0.1:443"
PVC = f"{API}/api/v1/namespaces/cms/persistentvolumeclaims/af-shared-storage"
PODS = f"{API}/api/v1/namespaces/cms/pods"
INGRESSES = f"{API}/apis/networking.k8s.io/v1/namespaces/cms/ingresses"
SERVICES = f"{API}/api/v1/namespaces/cms/services"

GRPC_HOST = "supersonic.geddes.rcac.purdue.edu"
GRAFANA_HOST = "supersonic-grafana.geddes.rcac.purdue.edu"
GRAFANA_LABELS = {"app.kubernetes.io/name": "grafana"}


def route(url, *items, body=None):
    """Serve ``items`` as a Kubernetes list, or ``body`` as a single object."""
    payload = {"items": list(items)} if body is None else body
    return respx.get(url).mock(return_value=httpx.Response(200, json=payload))


def ingress(name, host=None, tls=True, labels=None):
    metadata = {"name": name}
    if labels:
        metadata["labels"] = labels
    spec = {"rules": [{"host": host}] if host else []}
    if tls:
        spec["tls"] = [{"hosts": [host]}]
    return {"metadata": metadata, "spec": spec}


def service(name, *ports):
    spec = {"ports": [{"name": n, "port": p} for n, p in ports]} if ports else {}
    return {"metadata": {"name": name}, "spec": spec}


def pod(name, **status):
    return {"metadata": {"name": name}, "status": status}


@pytest.fixture
def in_cluster(tmp_path, monkeypatch):
    """Pretend a service account is mounted and the API server is reachable."""
    token = tmp_path / "token"
    token.write_text("tok\n")
    monkeypatch.setattr(kube, "TOKEN_FILE", token)
    monkeypatch.setattr(kube, "CA_FILE", tmp_path / "absent-ca.crt")
    monkeypatch.setattr(settings, "kubernetes_host", "10.0.0.1")
    monkeypatch.setattr(settings, "kubernetes_port", "443")
    monkeypatch.setattr(settings, "namespace", "cms")
    monkeypatch.setattr(settings, "triton_namespace", "cms")
    monkeypatch.setattr(settings, "pvc_name", "af-shared-storage")
    monkeypatch.setattr(settings, "supersonic_release", "supersonic")
    monkeypatch.setattr(settings, "inference_endpoint", "")
    monkeypatch.setattr(settings, "grafana_url", "")
    monkeypatch.setattr(settings, "triton_label_selector", "app=triton")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1Gi", 1024**3),
        ("50Gi", 50 * 1024**3),
        ("40Ti", 40 * 1024**4),
        ("500M", 500 * 10**6),
        ("1", 1),
        ("", 0),
        (None, 0),
        ("garbage", 0),
        ("1.2.3Gi", 0),
    ],
)
def test_parse_quantity(value, expected):
    assert kube.parse_quantity(value) == expected


def test_api_unavailable_outside_a_cluster(monkeypatch):
    monkeypatch.setattr(settings, "kubernetes_host", "")

    assert kube.api_available() is False
    assert kube.list_triton_pods() == []
    assert kube.pvc_capacity_bytes() == 0


@respx.mock
@pytest.mark.parametrize(
    "body,expected",
    [
        pytest.param(
            {
                "status": {"capacity": {"storage": "40Ti"}},
                "spec": {"resources": {"requests": {"storage": "1Gi"}}},
            },
            40 * 1024**4,
            id="prefers-bound-status",
        ),
        pytest.param(
            {"status": {}, "spec": {"resources": {"requests": {"storage": "50Gi"}}}},
            50 * 1024**3,
            id="falls-back-to-the-request",
        ),
    ],
)
def test_pvc_capacity(in_cluster, body, expected):
    route(PVC, body=body)

    assert kube.pvc_capacity_bytes() == expected


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            {"return_value": httpx.Response(403, text="forbidden")}, id="http-error"
        ),
        pytest.param(
            {"side_effect": httpx.ConnectError("refused")}, id="connection-failure"
        ),
    ],
)
def test_api_failures_are_swallowed(in_cluster, failure):
    respx.get(PVC).mock(**failure)

    assert kube.pvc_capacity_bytes() == 0


def test_unreadable_token_means_no_api_call(in_cluster, monkeypatch):
    class UnreadableToken:
        def is_file(self):
            return True

        def read_text(self):
            raise PermissionError("denied")

    monkeypatch.setattr(kube, "TOKEN_FILE", UnreadableToken())

    with respx.mock(assert_all_called=False) as router:
        unreached = router.get(url__startswith=API)
        assert kube.pvc_capacity_bytes() == 0

    assert not unreached.called


def test_no_pvc_name_means_no_capacity(in_cluster, monkeypatch):
    monkeypatch.setattr(settings, "pvc_name", "")

    assert kube.pvc_capacity_bytes() == 0


@respx.mock
def test_requests_carry_the_service_account_token(in_cluster):
    pvc = route(PVC, body={})

    kube.pvc_capacity_bytes()

    assert pvc.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
def test_lists_only_addressable_pods(in_cluster):
    route(
        PODS,
        pod("triton-1", podIP="10.1.1.2", phase="Running"),
        pod("triton-0", phase="Pending"),
        pod("triton-old", podIP="10.1.1.9", phase="Succeeded"),
    )

    assert kube.list_triton_pods() == [{"name": "triton-1", "ip": "10.1.1.2"}]


def test_explicit_endpoint_skips_discovery(in_cluster, monkeypatch):
    monkeypatch.setattr(settings, "inference_endpoint", "sonic.example.org:443")

    assert kube.find_inference_endpoint() == {
        "endpoint": "sonic.example.org:443",
        "source": "configured",
    }


@respx.mock
@pytest.mark.parametrize(
    "ingresses,endpoint",
    [
        pytest.param(
            [
                ingress("supersonic-grafana", GRAFANA_HOST),
                ingress("supersonic-ingress-grpc", GRPC_HOST),
            ],
            f"{GRPC_HOST}:443",
            id="prefers-grpc-over-other-release-ingresses",
        ),
        pytest.param(
            [ingress("supersonic-ingress-grpc", "sonic.local", tls=False)],
            "sonic.local:80",
            id="without-tls-uses-port-80",
        ),
    ],
)
def test_inference_endpoint_from_the_grpc_ingress(in_cluster, ingresses, endpoint):
    route(INGRESSES, *ingresses)

    assert kube.find_inference_endpoint() == {
        "endpoint": endpoint,
        "source": "ingress",
    }


@respx.mock
@pytest.mark.parametrize(
    "ingresses,services,port",
    [
        pytest.param(
            [],
            [service("supersonic", ("admin", 9901), ("grpc", 8001))],
            8001,
            id="no-ingress-grpc-port-last",
        ),
        pytest.param(
            [ingress("supersonic-grafana", GRAFANA_HOST)],
            [service("supersonic", ("grpc", 8001), ("admin", 9901))],
            8001,
            id="only-grafana-ingressed-grpc-port-first",
        ),
        pytest.param(
            [{"metadata": {"name": "supersonic-ingress-grpc"}, "spec": {}}],
            [service("noports"), service("supersonic", ("http", 8000))],
            8000,
            id="grpc-ingress-without-a-host",
        ),
    ],
)
def test_inference_endpoint_falls_back_to_the_envoy_service(
    in_cluster, ingresses, services, port
):
    route(INGRESSES, *ingresses)
    route(SERVICES, *services)

    assert kube.find_inference_endpoint() == {
        "endpoint": f"supersonic.cms.svc.cluster.local:{port}",
        "source": "service",
    }


@respx.mock
def test_no_endpoint_when_nothing_is_found(in_cluster):
    route(INGRESSES)
    route(SERVICES)

    assert kube.find_inference_endpoint() == {"endpoint": "", "source": None}


def test_no_endpoint_without_a_release_name(in_cluster, monkeypatch):
    monkeypatch.setattr(settings, "supersonic_release", "")

    assert kube.find_inference_endpoint()["endpoint"] == ""


def test_explicit_grafana_url_skips_discovery(in_cluster, monkeypatch):
    monkeypatch.setattr(settings, "grafana_url", "https://grafana.example.org")

    assert kube.find_grafana_url() == {
        "url": "https://grafana.example.org",
        "source": "configured",
    }


@respx.mock
@pytest.mark.parametrize(
    "ingresses,expected",
    [
        pytest.param(
            [
                ingress("supersonic-ingress-grpc", GRPC_HOST),
                ingress("supersonic-grafana", GRAFANA_HOST, labels=GRAFANA_LABELS),
            ],
            {"url": f"https://{GRAFANA_HOST}", "source": "ingress"},
            id="grafana-ingress",
        ),
        pytest.param(
            [ingress("supersonic-ingress-grpc", GRPC_HOST)],
            {"url": "", "source": None},
            id="no-grafana-ingress",
        ),
        pytest.param(
            [
                ingress("supersonic-grafana", tls=False),
                ingress("dashboards", "dash.local", tls=False, labels=GRAFANA_LABELS),
            ],
            {"url": "http://dash.local", "source": "ingress"},
            id="found-by-label-without-tls-or-host",
        ),
    ],
)
def test_grafana_url_from_the_release_ingresses(in_cluster, ingresses, expected):
    route(INGRESSES, *ingresses)

    assert kube.find_grafana_url() == expected


def test_no_grafana_url_without_a_release_name(in_cluster, monkeypatch):
    monkeypatch.setattr(settings, "supersonic_release", "")

    assert kube.find_grafana_url() == {"url": "", "source": None}
