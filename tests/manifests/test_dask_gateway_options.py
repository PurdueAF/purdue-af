"""The options handler the Kubernetes Dask Gateway execs (gateway.extraConfig.config)."""

import sys
import types

import pytest
import yaml
from common import REPO, ConfigSink

VALUES = REPO / "apps" / "dask-gateway" / "dask-gateway-k8s" / "values.yaml"


@pytest.fixture
def options_handler(monkeypatch):
    """Exec the embedded config against stub dask_gateway_server modules."""
    options = types.ModuleType("dask_gateway_server.options")
    for name in ("Options", "Integer", "Float", "Mapping", "String", "Select"):
        setattr(options, name, lambda *args, **kwargs: None)
    kubernetes = types.ModuleType("dask_gateway_server.backends.kubernetes")
    kubernetes.KubeBackend = type("KubeBackend", (), {"start_cluster": None})
    base = types.ModuleType("dask_gateway_server.backends.base")
    base.PublicException = Exception
    package = types.ModuleType("dask_gateway_server")
    package.models = types.ModuleType("dask_gateway_server.models")
    for module in (options, kubernetes, base, package):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    code = yaml.safe_load(VALUES.read_text())["gateway"]["extraConfig"]["config"]
    ns = {"c": ConfigSink()}
    exec(compile(code, f"{VALUES}:gateway.extraConfig.config", "exec"), ns)
    return ns["options_handler"]


def test_env_names_kubernetes_rejects_are_dropped(options_handler):
    options = types.SimpleNamespace(
        env={
            "PATH": "/usr/bin",
            "X509_USER_PROXY": "/work/users/someone/x509up",
            "my.env-name": "kept",
            "BASH_FUNC_which%%": "() {  ( alias; eval ${which_declare} ) | /usr/bin/which $@\n}",
            "1_LEADING_DIGIT": "dropped",
        },
        conda_env="/opt/env",
        pixi_project="",
        pixi_env="default",
        worker_cores=1,
        worker_memory=4,
    )
    config = options_handler(options, types.SimpleNamespace(name="jovyan"))
    environment = config["environment"]
    assert "BASH_FUNC_which%%" not in environment
    assert "1_LEADING_DIGIT" not in environment
    assert environment["X509_USER_PROXY"] == "/work/users/someone/x509up"
    assert environment["my.env-name"] == "kept"
