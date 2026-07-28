"""Runtime configuration, read from the environment.

Every value has a usable default so the app can run locally (outside a cluster)
with nothing but ``MODEL_REPOSITORY_PATH`` pointed at a directory.
"""

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return max(value, minimum)


def _str(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or default).strip()


def _csv(name: str) -> list:
    raw = _str(name)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    # -- Model repository (the PVC mount) --------------------------------
    repository_path: str = field(
        default_factory=lambda: _str("MODEL_REPOSITORY_PATH", "/models")
    )
    pvc_name: str = field(
        default_factory=lambda: _str("PVC_NAME", "supersonic-model-repository")
    )
    namespace: str = field(default_factory=lambda: _str("POD_NAMESPACE", "default"))
    max_upload_bytes: int = field(
        default_factory=lambda: _int("MAX_UPLOAD_BYTES", 8 * 1024 * 1024 * 1024, 1024)
    )
    read_only: bool = field(default_factory=lambda: _bool("READ_ONLY", False))

    # -- Triton discovery -------------------------------------------------
    # "kubernetes" lists Triton pods by label; "static" uses TRITON_ENDPOINTS.
    triton_discovery: str = field(
        default_factory=lambda: _str("TRITON_DISCOVERY", "kubernetes")
    )
    triton_namespace: str = field(default_factory=lambda: _str("TRITON_NAMESPACE"))
    triton_label_selector: str = field(
        default_factory=lambda: _str(
            "TRITON_LABEL_SELECTOR", "app.kubernetes.io/component=triton"
        )
    )
    triton_http_port: int = field(
        default_factory=lambda: _int("TRITON_HTTP_PORT", 8000, 1)
    )
    triton_endpoints: list = field(default_factory=lambda: _csv("TRITON_ENDPOINTS"))
    triton_timeout_s: float = field(
        default_factory=lambda: _int("TRITON_TIMEOUT_S", 10, 1)
    )

    # -- Prometheus -------------------------------------------------------
    prometheus_url: str = field(default_factory=lambda: _str("PROMETHEUS_URL"))
    # Extra PromQL label matchers, e.g. 'release="sonic-interlink",namespace="sonic"'
    prometheus_selector: str = field(
        default_factory=lambda: _str("PROMETHEUS_SELECTOR")
    )
    prometheus_window: str = field(
        default_factory=lambda: _str("PROMETHEUS_WINDOW", "5m")
    )
    prometheus_timeout_s: float = field(
        default_factory=lambda: _int("PROMETHEUS_TIMEOUT_S", 8, 1)
    )

    # -- Auth -------------------------------------------------------------
    auth_enabled: bool = field(default_factory=lambda: _bool("AUTH_ENABLED", True))
    auth_username: str = field(default_factory=lambda: _str("AUTH_USERNAME", "admin"))
    auth_password: str = field(default_factory=lambda: _str("AUTH_PASSWORD"))

    # -- Kubernetes API (injected by the kubelet inside a cluster) --------
    kubernetes_host: str = field(
        default_factory=lambda: _str("KUBERNETES_SERVICE_HOST")
    )
    kubernetes_port: str = field(
        default_factory=lambda: (
            _str("KUBERNETES_SERVICE_PORT_HTTPS")
            or _str("KUBERNETES_SERVICE_PORT", "443")
        )
    )

    # -- Inference endpoint shown on the dashboard ------------------------
    # Name of the SuperSONIC release, used to find its Envoy ingress/service.
    supersonic_release: str = field(default_factory=lambda: _str("SUPERSONIC_RELEASE"))
    # Explicit override; skips discovery entirely.
    inference_endpoint: str = field(default_factory=lambda: _str("INFERENCE_ENDPOINT"))

    # -- Misc -------------------------------------------------------------
    instance_name: str = field(
        default_factory=lambda: _str("INSTANCE_NAME", "SuperSONIC")
    )
    refresh_seconds: int = field(default_factory=lambda: _int("REFRESH_SECONDS", 15, 3))

    def __post_init__(self):
        if not self.triton_namespace:
            self.triton_namespace = self.namespace
        if self.triton_discovery not in ("kubernetes", "static"):
            self.triton_discovery = "kubernetes"
        if self.triton_endpoints:
            # An explicit endpoint list always wins over label discovery.
            self.triton_discovery = "static"

    @property
    def auth_configured(self) -> bool:
        return bool(self.auth_enabled and self.auth_password)


settings = Settings()
