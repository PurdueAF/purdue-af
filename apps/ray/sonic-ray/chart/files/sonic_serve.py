"""Ray-native autoscaler for the Triton servers riding in the Ray worker pods.

Shipped to the Ray head pod in a ConfigMap and imported by Ray Serve as
``sonic_serve:app`` — see templates/rayservice.yaml.

Ray's autoscaler grows and shrinks the cluster from *Ray* resource demand, and
Triton is invisible to it: inference arrives over gRPC straight at the Triton
containers, never touching a Ray task or actor, so left alone the worker group
would sit at its floor no matter the load. This turns Triton's own view of its
queues into exactly that Ray demand::

    pending  = Σ nv_inference_pending_request_count over every live Triton,
               averaged over look_back_s
    desired  = clamp(ceil(pending / target_pending_per_server), min, max)
    request_resources(bundles=[{"triton": 1}] * desired)

``request_resources`` is the Ray autoscaler's public "keep room for this" API;
each worker advertises ``triton: 1``, so a bundle is a GPU pod with a Triton in
it, and the autoscaler adds and removes pods to match. Nothing else in the
cluster asks for that resource, which is what lets a pod above the floor go
idle and be reclaimed.

Scaling up is immediate. Scaling down releases one pod — and the Triton in
it — per ``downscale_delay_s`` of sustained low demand, so a lull between
batches costs at most one server. The request is re-asserted every tick, so a
restarted controller inherits the cluster it finds rather than a stale floor.

Tunables arrive through the deployment's ``user_config``, so retuning the
policy is a config change rather than a restart. ``GET /`` on the Serve port
returns the last decision and what it was based on.
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

# The Ray resource a worker advertises for the Triton it carries. Must match
# rayStartParams.resources in the RayService.
TRITON_RESOURCE = "triton"
# Triton's per-model gauge of requests awaiting execution.
PENDING_METRIC = "nv_inference_pending_request_count"

DEFAULTS: dict[str, Any] = {
    "min_servers": 1,
    "max_servers": 10,
    # Ray Serve's target_ongoing_requests, in Triton's currency.
    "target_pending_per_server": 16,
    # A single sample of a queue gauge is noise; average this many seconds.
    "look_back_s": 30.0,
    "control_interval_s": 10.0,
    "downscale_delay_s": 120.0,
    "downscale_step": 1,
    "metrics_port": 8002,
    "scrape_timeout_s": 3.0,
}


def sum_pending(metrics_text: str) -> float:
    """Total requests awaiting execution in one Triton's /metrics output."""
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


def decide(
    pending: float,
    requested: int,
    low_since: float | None,
    now: float,
    config: dict[str, Any],
) -> tuple[int, float | None]:
    """The policy, free of I/O: (servers to request, when demand went low).

    ``requested`` is what the controller currently asks for; ``low_since`` is
    when demand first dropped below it, or None while it has not.
    """
    lo, hi = int(config["min_servers"]), int(config["max_servers"])
    target = max(1.0, float(config["target_pending_per_server"]))
    desired = min(hi, max(lo, math.ceil(pending / target)))

    if desired > requested:
        # A queue is already forming; waiting makes it worse.
        return desired, None
    if desired < requested:
        low_since = now if low_since is None else low_since
        if now - low_since < float(config["downscale_delay_s"]):
            return requested, low_since
        # One step per delay window: every step kills a Triton.
        return max(desired, requested - max(1, int(config["downscale_step"]))), None
    return requested, None


@serve.deployment
class TritonAutoscaler:
    """One replica, pinned to the head by the ``controller`` resource.

    It holds no GPU and serves no inference — the Triton containers do that,
    and clients reach them through the release's triton Service directly.
    """

    def __init__(self) -> None:
        self._config = dict(DEFAULTS)
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {"state": "starting"}
        self._history: list[tuple[float, float]] = []
        self._low_since: float | None = None
        # Learned from the cluster on the first tick, so a restarted controller
        # neither shrinks nor grows the group just for having restarted.
        self._requested: int | None = None
        self._thread = threading.Thread(
            target=self._run, name="triton-autoscaler", daemon=True
        )
        self._thread.start()

    def reconfigure(self, config: dict[str, Any]) -> None:
        """Serve calls this with user_config, at startup and on every change."""
        with self._lock:
            self._config = {**DEFAULTS, **(config or {})}
        logger.info("autoscaler config: %s", self._config)

    async def __call__(self, request) -> dict[str, Any]:
        with self._lock:
            return {"config": dict(self._config), **self._status}

    # -- control loop -------------------------------------------------------

    def _snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._config)

    def _run(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - a bad tick must not end the loop
                logger.exception("autoscaler tick failed")
            time.sleep(float(self._snapshot()["control_interval_s"]))

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
            with urllib.request.urlopen(
                url, timeout=float(config["scrape_timeout_s"])
            ) as r:
                return sum_pending(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # Still loading models, or on its way out. It holds a GPU either
            # way, so it counts as a server; it just reports no load.
            logger.debug("no metrics from %s: %s", address, exc)
            return None

    def _tick(self) -> None:
        config = self._snapshot()
        now = time.monotonic()

        servers = self._servers()
        samples = [self._pending(address, config) for address in servers]
        reachable = [s for s in samples if s is not None]
        pending_now = sum(reachable)

        self._history.append((now, pending_now))
        self._history = [
            (t, p) for t, p in self._history if now - t <= float(config["look_back_s"])
        ]
        pending = sum(p for _, p in self._history) / len(self._history)

        if self._requested is None:
            lo, hi = int(config["min_servers"]), int(config["max_servers"])
            self._requested = min(hi, max(lo, len(servers)))
            logger.info(
                "found %d Triton server(s); starting from there", self._requested
            )

        requested, self._low_since = decide(
            pending, self._requested, self._low_since, now, config
        )
        if requested != self._requested:
            logger.info(
                "requesting %d Triton server(s) (was %d; pending %.1f over %ss)",
                requested,
                self._requested,
                pending,
                config["look_back_s"],
            )
        self._requested = requested

        # Every tick, not only on change: the request lives in GCS, and this
        # is the only thing that keeps it true.
        request_resources(bundles=[{TRITON_RESOURCE: 1}] * self._requested)

        with self._lock:
            self._status = {
                "state": "running",
                "servers": len(servers),
                "servers_reporting": len(reachable),
                "pending_requests_now": pending_now,
                "pending_requests_avg": round(pending, 2),
                "requested_servers": self._requested,
                "downscale_pending_for_s": (
                    round(now - self._low_since, 1) if self._low_since else 0.0
                ),
            }


app = TritonAutoscaler.bind()
