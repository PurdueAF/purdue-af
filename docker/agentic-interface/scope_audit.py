"""Counts the series query_prometheus would show someone who owns nothing that
still name a person, or another namespace, through a label promql_scope does
not cover. A count above zero means a label belongs in promql_scope."""

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any

import httpx
from config import CLUSTER_PROMETHEUS_URL, NAMESPACE, PROMETHEUS_URL
from prometheus_client import Gauge
from promql_scope import (
    IDENTITY_LABELS,
    NAMESPACE_LABELS,
    OBJECT_LABELS,
    SHARED_PREFIXES,
    USER_PREFIXES,
    Owned,
    scope,
)

logger = logging.getLogger(__name__)

SOURCES = {"af": PROMETHEUS_URL, "cluster": CLUSTER_PROMETHEUS_URL}
INTERVAL = 6 * 3600
WINDOW = 3600
TIMEOUT = 60.0
COVERED = {"__name__", *NAMESPACE_LABELS, *IDENTITY_LABELS, *OBJECT_LABELS}
LEGACY_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
NAMES_A_PERSON = ".*(?:" + "|".join(re.escape(p) for p in USER_PREFIXES) + ").*"
SHARED = "|".join(f".*{re.escape(p)}.*" for p in SHARED_PREFIXES)

UNSCOPED = Gauge(
    "purdue_af_mcp_promql_unscoped_series",
    "Series query_prometheus shows that name a person or another namespace "
    "through a label promql_scope does not cover",
    ["source", "label"],
)
AUDITED = Gauge(
    "purdue_af_mcp_promql_audit_timestamp_seconds",
    "When the query_prometheus scope audit last finished",
)


def _label(name: str) -> str:
    return name if LEGACY_NAME.fullmatch(name) else json.dumps(name)


def checks(label: str) -> list[str]:
    """Selectors for a label's values that name a person's object or another namespace."""
    name = _label(label)
    found = [
        f'{{__name__=~".+",{name}=~{json.dumps(NAMES_A_PERSON)},'
        f"{name}!~{json.dumps(SHARED)}}}"
    ]
    if label.endswith("namespace"):
        found.append(f'{{__name__=~".+",{name}!~{json.dumps("|" + NAMESPACE)}}}')
    return found


# POST: a scoped selector is longer than the Rancher proxy takes in a URL.
async def _post(client: httpx.AsyncClient, url: str, path: str, **form: str) -> Any:
    resp = await client.post(f"{url}{path}", data=form, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()["data"]


async def audit(client: httpx.AsyncClient) -> dict[tuple[str, str], int]:
    """(source, label) -> how many series a label leaves visible."""
    nobody = Owned("")
    end = time.time()
    visible = await scope('{__name__=~".+"}', nobody)
    found: dict[tuple[str, str], int] = {}
    for source, url in SOURCES.items():
        labels = await _post(
            client,
            url,
            "/api/v1/labels",
            **{"match[]": visible, "start": f"{end - WINDOW:.3f}"},
        )
        for label in labels:
            if label in COVERED:
                continue
            for check in checks(label):
                query = await scope(f"count({check})", nobody)
                result = await _post(client, url, "/api/v1/query", query=query)
                count = (
                    int(float(result["result"][0]["value"][1]))
                    if result["result"]
                    else 0
                )
                if count:
                    found[(source, label)] = found.get((source, label), 0) + count
    return found


async def _forever() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            try:
                found = await audit(client)
            except Exception:
                logger.exception("query_prometheus scope audit failed")
            else:
                UNSCOPED.clear()
                for (source, label), count in found.items():
                    UNSCOPED.labels(source=source, label=label).set(count)
                    logger.warning(
                        "query_prometheus shows %d series naming a person or "
                        "another namespace through %s on %s",
                        count,
                        label,
                        source,
                    )
                AUDITED.set_to_current_time()
            await asyncio.sleep(INTERVAL)


def start() -> None:
    threading.Thread(
        target=lambda: asyncio.run(_forever()), name="scope-audit", daemon=True
    ).start()
