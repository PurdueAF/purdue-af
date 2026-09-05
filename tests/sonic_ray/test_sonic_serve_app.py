"""The Ray Serve layer, checked without Ray or tritonclient (neither is a test
dependency): the module is read as source. What matters is small — it must
forward every unary RPC of Triton's service and nothing else, be importable
under the name the chart uses, and gate readiness on Triton's."""

import ast
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


@pytest.fixture(scope="module")
def source():
    return SERVE_APP.read_text()


@pytest.fixture(scope="module")
def module(source):
    return ast.parse(source)


def test_forwards_every_rpc_but_the_bidirectional_stream(source):
    """The RPC list is derived from Triton's generated servicer at import, so
    a new Triton RPC is forwarded without an edit here; the one stream Serve
    cannot carry is the only exclusion."""
    assert "vars(service_pb2_grpc.GRPCInferenceServiceServicer)" in source
    assert 'name != "ModelStreamInfer"' in source
    assert "setattr(TritonProxy, _rpc, _forwarder(_rpc))" in source
    assert "await getattr(self._stub, rpc)(request)" in source


def test_bound_under_the_name_the_chart_imports(module):
    """serveConfigV2's import_path is `sonic_ray.serve_app:triton`."""
    bound = next(
        n
        for n in module.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "triton" for t in n.targets)
    )
    assert ast.unparse(bound.value) == "serve.deployment(TritonProxy).bind()"


def test_replica_readiness_is_tritons(module):
    """A replica that came up before its Triton finished loading would be
    routed to; __init__ blocks on ServerReady and check_health polls ServerLive."""
    cls = next(
        n
        for n in module.body
        if isinstance(n, ast.ClassDef) and n.name == "TritonProxy"
    )
    methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert {"__init__", "_wait_for_triton", "check_health"} <= methods
    init = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    assert "self._wait_for_triton()" in ast.unparse(init)


def test_nothing_here_parses_a_request(source):
    """Triton does the inference; this file must stay a pass-through."""
    for forbidden in ("numpy", "onnxruntime", "fastapi", "json", "InferInput"):
        assert forbidden not in source, forbidden
