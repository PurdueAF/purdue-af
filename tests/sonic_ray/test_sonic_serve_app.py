"""The Ray Serve layer, checked without Ray: the module is read as source,
since `ray` is not a test dependency and the interesting logic lives in
models.py, which test_sonic_models.py exercises directly."""

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

ENDPOINTS = {
    ("GET", "/healthz"),
    ("GET", "/models"),
    ("GET", "/models/{model_name}"),
    ("POST", "/models/{model_name}"),
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


def test_the_documented_endpoints_are_routed(server_class):
    assert routes(server_class) == ENDPOINTS


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


def test_inference_runs_off_the_event_loop():
    """ORT releases the GIL; running it in a thread is what lets one replica
    serve concurrent requests to different models."""
    assert re.search(r"await asyncio\.to_thread\(model\.infer", SERVE_APP.read_text())


def test_configuration_is_by_environment():
    """The chart sets these on every container (templates/_helpers.tpl); the
    names must match what the code reads."""
    source = SERVE_APP.read_text()
    for var in ("MODEL_REPOSITORY", "ONNX_EXECUTION_PROVIDERS", "LOG_LEVEL"):
        assert f'"{var}"' in source, var
    assert '"MODELS"' not in source, (
        "the allowlist is gone; the chart no longer sets it"
    )
