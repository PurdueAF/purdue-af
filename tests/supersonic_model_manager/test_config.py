"""Settings read from the environment."""

from model_manager.config import Settings


def test_environment_overrides_defaults(monkeypatch):
    monkeypatch.setenv("READ_ONLY", " Yes ")
    monkeypatch.setenv("AUTO_LOAD_ON_UPLOAD", "off")
    monkeypatch.setenv("REFRESH_SECONDS", "1")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "4096")
    monkeypatch.setenv("POD_NAMESPACE", "cms")

    s = Settings()

    assert s.read_only is True
    assert s.auto_load_on_upload is False
    assert s.refresh_seconds == 3, "clamped to the minimum"
    assert s.max_upload_bytes == 4096
    assert s.triton_namespace == "cms", "defaults to the pod's namespace"


def test_garbage_numbers_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("REFRESH_SECONDS", "soon")

    assert Settings().refresh_seconds == 15


def test_unknown_discovery_mode_falls_back_to_kubernetes(monkeypatch):
    monkeypatch.setenv("TRITON_DISCOVERY", "dns")
    monkeypatch.delenv("TRITON_ENDPOINTS", raising=False)

    assert Settings().triton_discovery == "kubernetes"


def test_explicit_endpoints_force_static_discovery(monkeypatch):
    monkeypatch.setenv("TRITON_DISCOVERY", "kubernetes")
    monkeypatch.setenv("TRITON_ENDPOINTS", " a:8000, ,b ")

    s = Settings()

    assert s.triton_discovery == "static"
    assert s.triton_endpoints == ["a:8000", "b"]


def test_auth_is_configured_only_with_a_password(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    assert Settings().auth_configured is False

    monkeypatch.setenv("AUTH_PASSWORD", "s3cret")
    assert Settings().auth_configured is True

    monkeypatch.setenv("AUTH_ENABLED", "false")
    assert Settings().auth_configured is False
