"""Per-model inference metrics from SuperSONIC's Prometheus.

All queries are instant queries aggregated by Triton's ``model`` label, so the
numbers cover every replica of every model regardless of which server the
dashboard happens to reach.
"""

import asyncio
import logging
import math

import httpx

from .config import settings

log = logging.getLogger(__name__)


def _selector() -> str:
    return "{%s}" % settings.prometheus_selector if settings.prometheus_selector else ""


def _queries() -> dict:
    sel = _selector()
    window = settings.prometheus_window
    success_rate = f"sum by (model) (rate(nv_inference_request_success{sel}[{window}]))"
    return {
        # inferences per second
        "throughput": success_rate,
        # average microseconds a request spends queued, per request
        "queueLatencyUs": (
            f"sum by (model) (rate(nv_inference_queue_duration_us{sel}[{window}])) / {success_rate}"
        ),
        # average microseconds of actual inference compute, per request
        "computeLatencyUs": (
            f"sum by (model) (rate(nv_inference_compute_infer_duration_us{sel}[{window}]))"
            f" / {success_rate}"
        ),
        # cumulative counters
        "inferenceCount": f"sum by (model) (nv_inference_request_success{sel})",
        "failureCount": f"sum by (model) (nv_inference_request_failure{sel})",
        # average requests per execution — how well batching is working
        "batchRatio": (
            f"{success_rate} / sum by (model) (rate(nv_inference_exec_count{sel}[{window}]))"
        ),
    }


def _finite(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


async def _instant_query(client: httpx.AsyncClient, query: str) -> dict:
    """Run one instant query, returning ``{model: value}``."""
    response = await client.get(
        f"{settings.prometheus_url.rstrip('/')}/api/v1/query", params={"query": query}
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(payload.get("error", "Prometheus query failed"))

    out = {}
    for series in payload.get("data", {}).get("result", []):
        model = series.get("metric", {}).get("model")
        if not model:
            continue
        value = _finite((series.get("value") or [None, None])[1])
        if value is not None:
            out[model] = value
    return out


async def collect_metrics() -> dict:
    """``{"models": {name: {metric: value}}, "error": str | None}``."""
    if not settings.prometheus_url:
        return {"models": {}, "error": None, "configured": False}

    queries = _queries()
    try:
        async with httpx.AsyncClient(timeout=settings.prometheus_timeout_s) as client:
            results = await asyncio.gather(
                *(_instant_query(client, q) for q in queries.values()),
                return_exceptions=True,
            )
    except Exception as exc:  # pragma: no cover - client construction only
        return {"models": {}, "error": str(exc), "configured": True}

    models = {}
    errors = []
    for key, result in zip(queries.keys(), results):
        if isinstance(result, Exception):
            errors.append(f"{key}: {type(result).__name__}")
            continue
        for model, value in result.items():
            models.setdefault(model, {})[key] = value

    return {
        "models": models,
        "error": "; ".join(errors) if errors else None,
        "configured": True,
        "window": settings.prometheus_window,
    }
