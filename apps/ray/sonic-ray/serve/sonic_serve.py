"""Ray-native autoscaler for the Triton servers riding in the Ray worker pods.

Mounted into the Ray head pod from a ConfigMap and imported by Ray Serve as
``sonic_serve:app`` — see apps/ray/sonic-ray/rayservice.yaml.

Ray's autoscaler grows and shrinks the cluster from *Ray* resource demand, and
Triton is invisible to it: the inference traffic arrives over gRPC straight at
the Triton containers, never touching a Ray task or actor, so left alone the
worker group would sit at its floor no matter the load. This turns Triton's own
view of its queues into exactly that Ray demand::

    pending  = Σ nv_inference_pending_request_count over every live Triton
    desired  = clamp(ceil(pending / target_pending_per_server), min, max)
    request_resources(bundles=[{"triton": 1}] * desired)

``request_resources`` is the Ray autoscaler's public "make room for this" API;
each worker advertises ``triton: 1``, so a bundle is a GPU pod with a Triton in
it, and the autoscaler adds and removes pods to match. Nothing else in the
cluster asks for that resource, which is what lets nodes above the floor go
idle and be reclaimed.

Scaling up is immediate. Scaling down waits for ``downscale_delay_s`` of
sustained low demand, because the alternative is releasing a pod — and killing
the Triton inside it — during a lull between batches.

Tunables live in the deployment's ``user_config`` in the RayService, so
retuning the policy is a config change rather than a restart. ``GET /`` on the
Serve port returns the last decision and what it was based on.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import ray
from ray import serve
from ray.autoscaler.sdk import request_resources

logger = logging.getLogger("ray.serve")

# The Ray resource a worker advertises for the Triton it carries.
TRITON_RESOURCE = "triton"
# Triton's Prometheus gauge of requests queued or executing, per model.
PENDING_METRIC = "nv_inference_pending_request_count"

DEFAULTS: dict[str, Any] = {
    "min_servers": 1,
    "max_servers": 10,
    # Ray Serve's target_ongoing_requests, in Triton's currency.
    "target_pending_per_server": 16,
    "control_interval_s": 10.0,
    "downscale_delay_s": 300.0,
    "metrics_port": 8002,
    "scrape_timeout_s": 3.0,
}


def _sum_pending(metrics_text: str) -> float:
    """Total queued-or-executing requests in one Triton's /metrics output."""
    total = 0.0
    for line in metrics_text.splitlines():
        if not line.startswith(PENDING_METRIC):
            continue
        # nv_inference_pending_request_count{model="x",version="1"} 3
        _, _, value = line.rpartition(" ")
        try:
            total += float(value)
        except ValueError:  # a HELP/TYPE line for the same metric name
            continue
    return total


@serve.deployment
class TritonAutoscaler:
    """One replica, pinned to the head by the ``controller`` resource.

    It holds no GPU and serves no inference — the Triton containers do that,
    and clients reach them through the sonic-ray-triton Service directly.
    """

    def __init__(self) -> None:
        self._config = dict(DEFAULTS)
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {"state": "starting"}
        self._low_since: float | None = None
        self._requested = int(DEFAULTS["min_servers"])
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="triton-autoscaler", daemon=True
        )
        self._thread.start()

    def reconfigure(self, config: dict[str, Any]) -> None:
        """Serve calls this with user_config, at startup and on every change."""
        with self._lock:
            self._config = {**DEFAULTS, **(config or {})}
        logger.info("autoscaler config: %s", self._config)

    async def __call__(self, request) -> dict[str, Any]:  # noqa: ARG002 - status only
        with self._lock:
            return {"config": dict(self._config), **self._status}

    # -- control loop -------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(self._snapshot()["control_interval_s"]):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - a bad tick must not end the loop
                logger.exception("autoscaler tick failed")

    def _snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._config)

    def _servers(self) -> list[str]:
        """Addresses of the live workers carrying a Triton."""
        return [
            node["NodeManagerAddress"]
            for node in ray.nodes()
            if node.get("Alive") and node.get("Resources", {}).get(TRITON_RESOURCE)
        ]

    def _pending(self, address: str, config: dict[str, Any]) -> float | None:
        url = f"http://{address}:{int(config['metrics_port'])}/metrics"
        try:
            with urllib.request.urlopen(url, timeout=config["scrape_timeout_s"]) as r:
                return _sum_pending(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # Still starting, or on its way out. Counted as a server (it holds a
            # GPU either way) but contributing no load.
            logger.debug("no metrics from %s: %s", address, exc)
            return None

    def _tick(self) -> None:
        config = self._snapshot()
        servers = self._servers()
        samples = [self._pending(address, config) for address in servers]
        reachable = [s for s in samples if s is not None]
        pending = sum(reachable)

        target = max(1.0, float(config["target_pending_per_server"]))
        desired = math.ceil(pending / target) if pending else int(config["min_servers"])
        desired = max(
            int(config["min_servers"]), min(int(config["max_servers"]), desired)
        )

        now = time.monotonic()
        if desired > self._requested:
            # A queue is already forming; waiting makes it worse.
            self._apply(desired)
            self._low_since = None
        elif desired < self._requested:
            self._low_since = self._low_since or now
            if now - self._low_since >= config["downscale_delay_s"]:
                self._apply(desired)
                self._low_since = None
        else:
            self._low_since = None

        with self._lock:
            self._status = {
                "state": "running",
                "servers": len(servers),
                "servers_reporting": len(reachable),
                "pending_requests": pending,
                "requested_servers": self._requested,
                "desired_servers": desired,
                "downscale_pending_for_s": (
                    round(now - self._low_since, 1) if self._low_since else 0.0
                ),
            }

    def _apply(self, desired: int) -> None:
        logger.info("requesting %d Triton server(s) (was %d)", desired, self._requested)
        request_resources(bundles=[{TRITON_RESOURCE: 1}] * desired)
        self._requested = desired


app = TritonAutoscaler.bind()
