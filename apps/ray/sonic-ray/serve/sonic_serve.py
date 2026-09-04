"""Ray Serve front door for the Triton server co-located in each worker pod.

Mounted into the Ray pods from a ConfigMap and imported by Ray Serve as
``sonic_serve:app`` — see apps/ray/sonic-ray/rayservice.yaml.

Every worker pod runs the production Triton image beside the Ray container, so
this is a *transparent* HTTP reverse proxy rather than a reimplementation of
anything: it forwards the method, path, query, headers and body byte-for-byte
to Triton on localhost and returns the response the same way. Whatever Triton
serves, this serves — every backend, every endpoint (``/v2/...``, model
control, statistics, tracing, logging), and the binary tensor extension, whose
``Inference-Header-Content-Length`` header rides through untouched.

Two things it is deliberately *not*:

- **Not the gRPC path.** CMSSW's SONIC clients speak gRPC, and Ray Serve's gRPC
  ingress routes by an ``application`` metadata key that a stock Triton client
  never sends. gRPC therefore reaches the Triton containers directly through
  the sonic-ray-triton Service; only HTTP comes through here.
- **Not a load balancer of its own.** Serve routes each request to a replica
  and the replica talks to *its own* pod's Triton, which is what makes replica
  count and Triton count the same number — and what lets Serve's autoscaler add
  worker pods, each of which brings a Triton with it.
"""

from __future__ import annotations

import logging
import os
import time

import aiohttp
from ray import serve
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("ray.serve")

TRITON_HTTP = os.environ.get("TRITON_HTTP", "http://127.0.0.1:8000")

# Triton binds its HTTP endpoint only after the initial --load-model pass, so a
# replica that starts beside a cold Triton has nothing to health-check for as
# long as the repository takes to load. Failing during that window would have
# Serve restart the replica, which restarts nothing that matters and hides the
# real state behind a crash loop.
STARTUP_GRACE_S = float(os.environ.get("TRITON_STARTUP_GRACE_S", 900))

# Headers that describe *this* connection rather than the message, plus the
# two whose values are recomputed by whoever sends the next message.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
    }
)


def _forwardable(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


@serve.deployment
class TritonProxy:
    """One replica per worker pod, fronting that pod's Triton.

    The pairing is enforced by the ``triton`` custom resource: each worker
    advertises exactly one, and each replica claims one, so a replica can only
    ever be placed on a pod that has a Triton to talk to — and asking for
    another replica is what makes the Ray autoscaler add another pod.
    """

    def __init__(self, endpoint: str = TRITON_HTTP, timeout_s: float = 300.0):
        self._endpoint = endpoint.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None
        self._deadline = time.monotonic() + STARTUP_GRACE_S
        self._was_healthy = False
        logger.info("proxying to %s", self._endpoint)

    async def _client(self) -> aiohttp.ClientSession:
        # Built on first use: a ClientSession binds to the running event loop,
        # which does not exist yet while the replica is being constructed.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                # Triton sets Content-Encoding itself when asked to; decoding
                # here would leave the header describing a body we changed.
                auto_decompress=False,
            )
        return self._session

    async def __call__(self, request: Request) -> Response:
        url = f"{self._endpoint}{request.url.path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        session = await self._client()
        try:
            async with session.request(
                request.method,
                url,
                headers=_forwardable(request.headers),
                data=await request.body(),
            ) as upstream:
                body = await upstream.read()
                return Response(
                    content=body,
                    status_code=upstream.status,
                    headers=_forwardable(upstream.headers),
                )
        except aiohttp.ClientError as exc:
            # The local Triton is down or still loading. 503 tells the client
            # to retry elsewhere; Serve's health check decides the replica's
            # fate separately.
            logger.warning("upstream %s failed: %s", url, exc)
            return Response(content=f"Triton unreachable: {exc}", status_code=503)

    async def check_health(self) -> None:
        """Serve restarts the replica if this raises.

        ``/v2/health/live`` and not ``/v2/health/ready``: a Triton busy loading
        a large model is not ready, but it is alive, and killing replicas for
        being slow to warm up is how a rollout turns into a loop. Traffic is
        gated by the pod's own readiness probe instead, which watches
        ``/v2/health/ready`` on the same container.

        Until the first success, failures are logged and tolerated — see
        STARTUP_GRACE_S. Afterwards a dead Triton is a dead replica, and Serve
        should replace it.
        """
        try:
            session = await self._client()
            async with session.get(f"{self._endpoint}/v2/health/live") as response:
                if response.status != 200:
                    raise RuntimeError(f"Triton liveness returned {response.status}")
        except Exception as exc:  # noqa: BLE001 - re-raised below once warmed up
            if self._was_healthy or time.monotonic() > self._deadline:
                raise
            logger.info("Triton not up yet (%s); still within startup grace", exc)
            return
        self._was_healthy = True


app = TritonProxy.bind()
