"""The Ray Serve layer, imported against stub `grpc`, `ray.serve` and
`tritonclient.grpc` modules (none is a test dependency). It must forward
every unary RPC of Triton's service unchanged, be bound under the name the
chart imports, and gate readiness on Triton's."""

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from common import load_script

SERVE_APP = (
    Path(__file__).resolve().parents[2]
    / "helm"
    / "sonic-ray"
    / "files"
    / "sonic_ray"
    / "serve_app.py"
)


class RpcError(Exception):
    pass


class Servicer:
    """The shape of Triton's generated servicer."""

    def __init__(self, request_iterator=None):
        pass

    def ServerLive(self, request, context): ...
    def ServerReady(self, request, context): ...
    def ModelInfer(self, request, context): ...
    def ModelMetadata(self, request, context): ...
    def RepositoryModelLoad(self, request, context): ...
    def ModelStreamInfer(self, request_iterator, context): ...


class Triton:
    """What the stubs talk to: scripted sync answers, recorded async calls."""

    def __init__(self):
        self.ready = []
        self.live = True
        self.forwarded = []
        self.channels = []

    def stub(self, channel):
        triton = self

        class SyncStub:
            def ServerReady(self, request, timeout):
                answer = triton.ready.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return SimpleNamespace(ready=answer)

            def ServerLive(self, request, timeout):
                return SimpleNamespace(live=triton.live)

        class AioStub:
            def __getattr__(self, rpc):
                async def call(request):
                    triton.forwarded.append((rpc, request))
                    return f"{rpc} reply"

                return call

        return AioStub() if channel[0] == "aio" else SyncStub()


@pytest.fixture
def triton():
    return Triton()


@pytest.fixture
def load(monkeypatch, triton):
    def channel(kind):
        def make(target, options):
            triton.channels.append((kind, target, dict(options)))
            return (kind, target)

        return make

    grpc = types.ModuleType("grpc")
    grpc.RpcError = RpcError
    grpc.insecure_channel = channel("sync")
    grpc.aio = SimpleNamespace(insecure_channel=channel("aio"))

    service_pb2 = SimpleNamespace(
        ServerReadyRequest=lambda: "ready?", ServerLiveRequest=lambda: "live?"
    )
    service_pb2_grpc = SimpleNamespace(
        GRPCInferenceServiceServicer=Servicer,
        GRPCInferenceServiceStub=triton.stub,
    )
    client = types.ModuleType("tritonclient.grpc")
    client.service_pb2 = service_pb2
    client.service_pb2_grpc = service_pb2_grpc

    serve = SimpleNamespace(
        deployment=lambda cls: SimpleNamespace(bind=lambda: ("bound", cls))
    )
    ray = types.ModuleType("ray")
    ray.serve = serve

    for name, module in {
        "grpc": grpc,
        "ray": ray,
        "ray.serve": serve,
        "tritonclient": types.ModuleType("tritonclient"),
        "tritonclient.grpc": client,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    def _load(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        module = load_script(SERVE_APP, "sonic_ray_serve_app")
        monkeypatch.delitem(sys.modules, "sonic_ray_serve_app")
        return module

    return _load


@pytest.fixture
def slept(monkeypatch):
    calls = []
    monkeypatch.setattr("time.sleep", calls.append)
    return calls


def test_forwards_every_rpc_but_the_bidirectional_stream(load, triton):
    """The list comes from Triton's servicer, so a new RPC needs no edit here;
    the one stream Serve cannot carry is the only exclusion."""
    app = load()
    triton.ready = [True]
    proxy = app.TritonProxy()

    assert set(app.RPCS) == {
        "ServerLive",
        "ServerReady",
        "ModelInfer",
        "ModelMetadata",
        "RepositoryModelLoad",
    }
    assert not hasattr(app.TritonProxy, "ModelStreamInfer")
    for rpc in app.RPCS:
        method = getattr(proxy, rpc)
        assert method.__name__ == rpc
        assert asyncio.run(method(f"{rpc} request")) == f"{rpc} reply"
    assert triton.forwarded == [(rpc, f"{rpc} request") for rpc in app.RPCS]


def test_channels_reach_the_local_triton_without_the_default_size_cap(load, triton):
    app = load(TRITON_GRPC="localhost:9001")
    triton.ready = [True]

    app.TritonProxy()

    assert [(kind, target) for kind, target, _ in triton.channels] == [
        ("sync", "localhost:9001"),
        ("aio", "localhost:9001"),
    ]
    for _, _, options in triton.channels:
        assert options["grpc.max_receive_message_length"] == 2**31 - 1
        assert options["grpc.max_send_message_length"] == 2**31 - 1


def test_bound_under_the_name_the_chart_imports(load):
    """serveConfigV2's import_path is `sonic_ray.serve_app:triton`."""
    app = load()
    assert app.triton == ("bound", app.TritonProxy)


def test_replica_waits_for_triton(load, triton, slept):
    app = load()
    triton.ready = [RpcError("connection refused"), False, True]

    app.TritonProxy()

    assert triton.ready == [] and slept == [2, 2]


@pytest.mark.parametrize("answer", [RpcError("connection refused"), False])
def test_replica_gives_up_on_a_triton_that_never_gets_ready(
    load, triton, slept, answer
):
    app = load(TRITON_READY_TIMEOUT_S="-1")
    triton.ready = [answer]

    with pytest.raises(RuntimeError, match="not ready after -1.0s"):
        app.TritonProxy()
    assert slept == []


def test_health_follows_triton_liveness(load, triton):
    app = load()
    triton.ready = [True]
    proxy = app.TritonProxy()

    proxy.check_health()
    triton.live = False
    with pytest.raises(RuntimeError, match="is not live"):
        proxy.check_health()


def test_nothing_here_parses_a_request():
    """Triton does the inference; this file must stay a pass-through."""
    source = SERVE_APP.read_text()
    for forbidden in ("numpy", "onnxruntime", "fastapi", "json", "InferInput"):
        assert forbidden not in source, forbidden
