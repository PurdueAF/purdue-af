"""Triton behind Ray Serve's gRPC proxy.

Every worker pod runs two containers: Triton (the SuperSONIC one — same image,
arguments, model repository) and Ray. This deployment runs on the Ray side,
one replica per pod (pinned there by the ``triton`` resource each worker
advertises), and forwards each RPC it receives to Triton on localhost.

Ray Serve's gRPC proxy is configured (in the chart's serveConfigV2) with
Triton's generated ``add_GRPCInferenceServiceServicer_to_server``, so it
accepts exactly Triton's protocol; it dispatches each call to the method of
this class with the RPC's name, and that method hands the protobuf message
to Triton and returns Triton's protobuf answer. Serve counts the request on
the way through, which is what its autoscaler and load balancing key on.

``ModelStreamInfer`` — Triton's one bidirectional stream — is not forwarded:
Serve's proxy carries unary and server-streaming calls only. CMSSW's client
uses the unary ``ModelInfer``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import grpc
from ray import serve
from tritonclient.grpc import service_pb2, service_pb2_grpc

LOGGER = logging.getLogger("sonic_ray")

# Triton's gRPC endpoint in this pod, and how long a fresh Triton may take to
# load the repository before the replica gives up on it.
TRITON = os.environ.get("TRITON_GRPC", "localhost:8001")
READY_TIMEOUT_S = float(os.environ.get("TRITON_READY_TIMEOUT_S", "900"))
# Inference payloads are large; match Serve's own proxy limit rather than
# grpc's 4 MB default.
MAX_MESSAGE_BYTES = 2**31 - 1
CHANNEL_OPTIONS = [
    ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
]

# Every RPC of Triton's service except the bidirectional stream.
RPCS = tuple(
    name
    for name in vars(service_pb2_grpc.GRPCInferenceServiceServicer)
    if not name.startswith("_") and name != "ModelStreamInfer"
)


class TritonProxy:
    """One replica = one pod = one Triton; every RPC goes to it unchanged."""

    def __init__(self) -> None:
        self._sync = service_pb2_grpc.GRPCInferenceServiceStub(
            grpc.insecure_channel(TRITON, options=CHANNEL_OPTIONS)
        )
        self._stub = service_pb2_grpc.GRPCInferenceServiceStub(
            grpc.aio.insecure_channel(TRITON, options=CHANNEL_OPTIONS)
        )
        self._wait_for_triton()

    def _wait_for_triton(self) -> None:
        """Block until Triton answers ServerReady: the replica is not ready
        until its Triton is, so Serve never routes to a still-loading pod."""
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            try:
                if self._sync.ServerReady(
                    service_pb2.ServerReadyRequest(), timeout=5
                ).ready:
                    LOGGER.info("triton at %s is ready", TRITON)
                    return
            except grpc.RpcError as exc:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"triton at {TRITON} not ready after {READY_TIMEOUT_S}s"
                    ) from exc
            time.sleep(2)

    def check_health(self) -> None:
        """Serve restarts the replica — and Ray then reclaims the pod — when
        its Triton stops answering."""
        if not self._sync.ServerLive(service_pb2.ServerLiveRequest(), timeout=5).live:
            raise RuntimeError(f"triton at {TRITON} is not live")


def _forwarder(rpc: str) -> Any:
    async def forward(self: TritonProxy, request: Any) -> Any:
        return await getattr(self._stub, rpc)(request)

    forward.__name__ = rpc
    return forward


for _rpc in RPCS:
    setattr(TritonProxy, _rpc, _forwarder(_rpc))

# What serveConfigV2's import_path points at: `sonic_ray.serve_app:triton`.
triton = serve.deployment(TritonProxy).bind()
