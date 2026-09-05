"""The Ray Serve layer, checked without Ray: the module is read as source,
since `ray` is not a test dependency and the interesting logic lives in the
two modules the other test files exercise directly."""

import ast
import re
from pathlib import Path

import pytest

SERVE_APP = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "ray"
    / "sonic-ray"
    / "chart"
    / "files"
    / "sonic_ray"
    / "serve_app.py"
)

# What a Triton HTTP client (tritonclient.http, CMSSW's or a curl) expects.
KSERVE_V2 = {
    ("GET", "/v2"),
    ("GET", "/v2/health/live"),
    ("GET", "/v2/health/ready"),
    ("GET", "/v2/models/{model_name}"),
    ("GET", "/v2/models/{model_name}/versions/{model_version}"),
    ("GET", "/v2/models/{model_name}/ready"),
    ("GET", "/v2/models/{model_name}/versions/{model_version}/ready"),
    ("POST", "/v2/models/{model_name}/infer"),
    ("POST", "/v2/models/{model_name}/versions/{model_version}/infer"),
    ("POST", "/v2/repository/index"),
}


@pytest.fixture(scope="module")
def module():
    return ast.parse(SERVE_APP.read_text())


@pytest.fixture(scope="module")
def server_class(module):
    return next(
        n
        for n in module.body
        if isinstance(n, ast.ClassDef) and n.name == "SonicServer"
    )


def routes(cls):
    found = set()
    for node in cls.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "app"
            ):
                found.add((decorator.func.attr.upper(), decorator.args[0].value))
    return found


def test_every_kserve_v2_endpoint_is_routed(server_class):
    assert KSERVE_V2 <= routes(server_class), KSERVE_V2 - routes(server_class)


def test_deployment_is_a_serve_ingress_and_bound_for_import(module, server_class):
    """serveConfigV2's import_path is `sonic_ray.serve_app:sonic`: the module
    must define `sonic` as the bound deployment of the class."""
    names = {
        d.func.value.id + "." + d.func.attr
        if isinstance(d, ast.Call)
        else d.value.id + "." + d.attr
        for d in server_class.decorator_list
    }
    assert {"serve.deployment", "serve.ingress"} <= names
    bound = next(
        n
        for n in module.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "sonic" for t in n.targets)
    )
    assert ast.unparse(bound.value) == "SonicServer.bind()"


def test_route_handlers_take_self_first(server_class):
    """serve.ingress binds `self`; a handler without it would be registered
    with the replica missing."""
    for node in server_class.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.decorator_list:
            assert node.args.args[0].arg == "self", node.name


def test_inference_runs_off_the_event_loop(module):
    """ORT releases the GIL; running it in a thread is what lets one replica
    serve concurrent requests to different models."""
    source = SERVE_APP.read_text()
    assert re.search(r"await asyncio\.to_thread\(model\.infer", source)


def test_configuration_is_by_environment(module):
    """The chart sets these on every container (templates/_helpers.tpl); the
    names must match what the code reads."""
    source = SERVE_APP.read_text()
    for var in ("MODEL_REPOSITORY", "ONNX_EXECUTION_PROVIDERS", "MODELS", "LOG_LEVEL"):
        assert f'"{var}"' in source, var
