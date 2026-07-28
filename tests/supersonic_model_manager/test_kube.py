"""Direct Kubernetes API access: quantities, pod discovery, inference endpoint."""

import httpx
import pytest
import respx
from model_manager import kube
from model_manager.config import settings

API = "https://10.0.0.1:443"


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
def test_pvc_capacity_prefers_bound_status(in_cluster):
    respx.get(
        f"{API}/api/v1/namespaces/cms/persistentvolumeclaims/af-shared-storage"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": {"capacity": {"storage": "40Ti"}},
                "spec": {"resources": {"requests": {"storage": "1Gi"}}},
            },
        )
    )

    assert kube.pvc_capacity_bytes() == 40 * 1024**4


@respx.mock
def test_pvc_capacity_falls_back_to_the_request(in_cluster):
    respx.get(
        f"{API}/api/v1/namespaces/cms/persistentvolumeclaims/af-shared-storage"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": {},
                "spec": {"resources": {"requests": {"storage": "50Gi"}}},
            },
        )
    )

    assert kube.pvc_capacity_bytes() == 50 * 1024**3


@respx.mock
def test_api_errors_are_swallowed(in_cluster):
    respx.get(
        f"{API}/api/v1/namespaces/cms/persistentvolumeclaims/af-shared-storage"
    ).mock(return_value=httpx.Response(403, text="forbidden"))

    assert kube.pvc_capacity_bytes() == 0


@respx.mock
def test_lists_only_pods_with_an_ip(in_cluster):
    respx.get(f"{API}/api/v1/namespaces/cms/pods").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {"name": "triton-1"},
                        "spec": {"nodeName": "node-a"},
                        "status": {
                            "podIP": "10.1.1.2",
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                        },
                    },
                    {  # pending, no IP yet — not addressable
                        "metadata": {"name": "triton-0"},
                        "spec": {},
                        "status": {"phase": "Pending"},
                    },
                    {  # finished
                        "metadata": {"name": "triton-old"},
                        "spec": {},
                        "status": {"podIP": "10.1.1.9", "phase": "Succeeded"},
                    },
                ]
            },
        )
    )

    pods = kube.list_triton_pods()

    assert [p["name"] for p in pods] == ["triton-1"]
    assert pods[0] == {
        "name": "triton-1",
        "ip": "10.1.1.2",
        "node": "node-a",
        "phase": "Running",
        "ready": True,
    }


# --------------------------------------------------------------------------
# Inference endpoint
# --------------------------------------------------------------------------


def ingress(name, host, tls=True):
    spec = {"rules": [{"host": host}]}
    if tls:
        spec["tls"] = [{"hosts": [host]}]
    return {"metadata": {"name": name}, "spec": spec}


def test_explicit_endpoint_skips_discovery(in_cluster, monkeypatch):
    monkeypatch.setattr(settings, "inference_endpoint", "sonic.example.org:443")

    assert kube.find_inference_endpoint() == {
        "endpoint": "sonic.example.org:443",
        "source": "configured",
    }


@respx.mock
def test_prefers_the_grpc_ingress_over_other_release_ingresses(in_cluster):
    respx.get(f"{API}/apis/networking.k8s.io/v1/namespaces/cms/ingresses").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    ingress(
                        "supersonic-grafana",
                        "supersonic-grafana.geddes.rcac.purdue.edu",
                    ),
                    ingress(
                        "supersonic-ingress-grpc", "supersonic.geddes.rcac.purdue.edu"
                    ),
                ]
            },
        )
    )

    result = kube.find_inference_endpoint()

    assert result == {
        "endpoint": "supersonic.geddes.rcac.purdue.edu:443",
        "source": "ingress",
    }


@respx.mock
def test_ingress_without_tls_uses_port_80(in_cluster):
    respx.get(f"{API}/apis/networking.k8s.io/v1/namespaces/cms/ingresses").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [ingress("supersonic-ingress-grpc", "sonic.local", tls=False)]
            },
        )
    )

    assert kube.find_inference_endpoint()["endpoint"] == "sonic.local:80"


@respx.mock
def test_falls_back_to_the_envoy_service_when_no_ingress(in_cluster):
    respx.get(f"{API}/apis/networking.k8s.io/v1/namespaces/cms/ingresses").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(f"{API}/api/v1/namespaces/cms/services").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {"name": "supersonic"},
                        "spec": {
                            "ports": [
                                {"name": "admin", "port": 9901},
                                {"name": "grpc", "port": 8001},
                            ]
                        },
                    }
                ]
            },
        )
    )

    assert kube.find_inference_endpoint() == {
        "endpoint": "supersonic.cms.svc.cluster.local:8001",
        "source": "service",
    }


@respx.mock
def test_ignores_non_grpc_ingresses_and_uses_the_envoy_service(in_cluster):
    # Current Geddes layout: only Grafana is Ingress-exposed; Envoy is a Service.
    respx.get(f"{API}/apis/networking.k8s.io/v1/namespaces/cms/ingresses").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    ingress(
                        "supersonic-grafana",
                        "supersonic-grafana.geddes.rcac.purdue.edu",
                    )
                ]
            },
        )
    )
    respx.get(f"{API}/api/v1/namespaces/cms/services").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {"name": "supersonic"},
                        "spec": {
                            "ports": [
                                {"name": "grpc", "port": 8001},
                                {"name": "admin", "port": 9901},
                            ]
                        },
                    }
                ]
            },
        )
    )

    assert kube.find_inference_endpoint() == {
        "endpoint": "supersonic.cms.svc.cluster.local:8001",
        "source": "service",
    }


@respx.mock
def test_no_endpoint_when_nothing_is_found(in_cluster):
    respx.get(f"{API}/apis/networking.k8s.io/v1/namespaces/cms/ingresses").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.get(f"{API}/api/v1/namespaces/cms/services").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    assert kube.find_inference_endpoint() == {"endpoint": "", "source": None}


def test_no_endpoint_without_a_release_name(in_cluster, monkeypatch):
    monkeypatch.setattr(settings, "supersonic_release", "")

    assert kube.find_inference_endpoint()["endpoint"] == ""
