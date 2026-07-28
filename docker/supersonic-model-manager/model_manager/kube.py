"""Minimal in-cluster Kubernetes API access.

Only two things are needed: discovering Triton pods and reading the model
repository PVC's capacity. That is two GETs against the API server, so this
talks to it directly with httpx instead of pulling in the official client —
which drags in google-auth and cryptography, a large dependency tree whose
Rust extension can abort the interpreter on import (SIGILL), something no
try/except can recover from.

Everything degrades to ``None``/empty when the app runs outside a cluster or
lacks RBAC, so the dashboard still works.
"""

import logging
import re
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger(__name__)

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TOKEN_FILE = SERVICE_ACCOUNT_DIR / "token"
CA_FILE = SERVICE_ACCOUNT_DIR / "ca.crt"

API_TIMEOUT_S = 10

_QUANTITY_RE = re.compile(r"^([0-9.]+)\s*([A-Za-z]*)$")
_SUFFIXES = {
    "": 1,
    "m": 0.001,
    "k": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}

_unavailable_logged = False


def parse_quantity(value) -> int:
    """Parse a Kubernetes resource quantity ('1Gi', '500M') into bytes."""
    if value is None:
        return 0
    match = _QUANTITY_RE.match(str(value).strip())
    if not match:
        return 0
    number, suffix = match.groups()
    try:
        return int(float(number) * _SUFFIXES.get(suffix, 1))
    except (ValueError, OverflowError):
        return 0


def _api_base() -> str:
    host = settings.kubernetes_host
    port = settings.kubernetes_port
    return f"https://{host}:{port}" if host else ""


def api_available() -> bool:
    """True when running inside a cluster with a mounted service account."""
    return bool(_api_base()) and TOKEN_FILE.is_file()


def _get(path: str, params=None):
    """GET a cluster resource, returning the decoded JSON or None."""
    global _unavailable_logged
    if not api_available():
        if not _unavailable_logged:
            log.info("Kubernetes API not available (running outside a cluster)")
            _unavailable_logged = True
        return None

    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError as exc:
        log.warning("Could not read the service account token: %s", exc)
        return None

    verify = str(CA_FILE) if CA_FILE.is_file() else True
    try:
        with httpx.Client(timeout=API_TIMEOUT_S, verify=verify) as client:
            response = client.get(
                f"{_api_base()}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        log.warning(
            "Kubernetes API %s -> HTTP %s: %s",
            path,
            exc.response.status_code,
            exc.response.text[:200],
        )
    except Exception as exc:
        log.warning("Kubernetes API %s failed: %s", path, exc)
    return None


def pvc_capacity_bytes() -> int:
    """Capacity the PVC reports as bound, falling back to its request."""
    if not settings.pvc_name:
        return 0

    payload = _get(
        f"/api/v1/namespaces/{settings.namespace}"
        f"/persistentvolumeclaims/{settings.pvc_name}"
    )
    if not payload:
        return 0

    capacity = ((payload.get("status") or {}).get("capacity") or {}).get("storage")
    if not capacity:
        resources = (payload.get("spec") or {}).get("resources") or {}
        capacity = (resources.get("requests") or {}).get("storage")
    return parse_quantity(capacity)


def find_inference_endpoint() -> dict:
    """Where clients send inference requests for this SuperSONIC release.

    Prefers the Envoy gRPC Ingress (what external clients use); falls back to
    the Envoy Service's in-cluster address.
    """
    if settings.inference_endpoint:
        return {"endpoint": settings.inference_endpoint, "source": "configured"}

    release = settings.supersonic_release
    if not release:
        return {"endpoint": "", "source": None}

    namespace = settings.triton_namespace
    selector = f"app.kubernetes.io/instance={release}"

    # SuperSONIC names the Envoy gRPC Ingress "{release}-ingress-grpc".
    # Other release ingresses (Grafana, metrics collector, …) share the
    # instance label and must be ignored.
    ingresses = _get(
        f"/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses",
        params={"labelSelector": selector},
    )
    for item in (ingresses or {}).get("items", []):
        name = (item.get("metadata") or {}).get("name", "")
        if "ingress-grpc" not in name:
            continue
        spec = item.get("spec") or {}
        rules = spec.get("rules") or []
        host = rules[0].get("host") if rules else None
        if not host:
            continue
        port = 443 if spec.get("tls") else 80
        return {"endpoint": f"{host}:{port}", "source": "ingress"}

    services = _get(
        f"/api/v1/namespaces/{namespace}/services",
        params={"labelSelector": f"{selector},app.kubernetes.io/component=envoy"},
    )
    for item in (services or {}).get("items", []):
        name = (item.get("metadata") or {}).get("name")
        ports = (item.get("spec") or {}).get("ports") or []
        grpc = next(
            (p for p in ports if p.get("name") == "grpc"),
            ports[0] if ports else None,
        )
        if name and grpc:
            return {
                "endpoint": f"{name}.{namespace}.svc.cluster.local:{grpc.get('port')}",
                "source": "service",
            }

    return {"endpoint": "", "source": None}


def find_grafana_url() -> dict:
    """Public URL of the SuperSONIC Grafana dashboard, if one is Ingress-exposed."""
    if settings.grafana_url:
        return {"url": settings.grafana_url, "source": "configured"}

    release = settings.supersonic_release
    if not release:
        return {"url": "", "source": None}

    namespace = settings.triton_namespace
    ingresses = _get(
        f"/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses",
        params={"labelSelector": f"app.kubernetes.io/instance={release}"},
    )
    for item in (ingresses or {}).get("items", []):
        meta = item.get("metadata") or {}
        name = meta.get("name", "")
        labels = meta.get("labels") or {}
        if "grafana" not in name and labels.get("app.kubernetes.io/name") != "grafana":
            continue
        spec = item.get("spec") or {}
        rules = spec.get("rules") or []
        host = rules[0].get("host") if rules else None
        if not host:
            continue
        scheme = "https" if spec.get("tls") else "http"
        return {"url": f"{scheme}://{host}", "source": "ingress"}

    return {"url": "", "source": None}


def list_triton_pods() -> list:
    """Triton pods as ``[{"name", "ip", "node", "phase", "ready"}]``."""
    payload = _get(
        f"/api/v1/namespaces/{settings.triton_namespace}/pods",
        params={"labelSelector": settings.triton_label_selector},
    )
    if not payload:
        return []

    result = []
    for pod in payload.get("items", []):
        status = pod.get("status") or {}
        pod_ip = status.get("podIP")
        if not pod_ip or status.get("phase") not in ("Running", "Pending"):
            continue
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions") or []
        )
        result.append(
            {
                "name": (pod.get("metadata") or {}).get("name", pod_ip),
                "ip": pod_ip,
                "node": (pod.get("spec") or {}).get("nodeName"),
                "phase": status.get("phase"),
                "ready": ready,
            }
        )
    result.sort(key=lambda p: p["name"])
    return result
