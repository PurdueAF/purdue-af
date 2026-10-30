"""The public dashboard's numbers, for questions a person would open it for.

It runs the dashboard's own stored queries and nothing else, so no caller can
choose what Prometheus is asked. A result reaches the caller only if every label
on it is in ``ALLOWED_LABELS``: a per-user or per-pod panel, present or added
later, is left out because of what it returns.
"""

import asyncio
import json
import re
from typing import Any, Optional

from config import CLUSTER_PROMETHEUS_URL, DASHBOARD_PATH, PROMETHEUS_URL
from errors import Failure, UpstreamError
from shared import prom_query, prom_vector, shared_client

# Grafana's datasource uid -> the shared client's name and the URL.
DATASOURCES = {
    "prometheus": ("prometheus", PROMETHEUS_URL),
    "prometheus-rancher": ("cluster-prometheus", CLUSTER_PROMETHEUS_URL),
}
ALLOWED_LABELS = {
    "code",
    "GPU_I_ID",
    "GPU_I_PROFILE",
    "gpu",
    "kubernetes_node",
    "mount_name",
    "node",
    "node_pool",
    "resource",
    "state",
    "type",
}
# Say nothing about who a series belongs to, and never reach the output.
DROPPED_LABELS = {"__name__", "instance", "job"}
WINDOW = "1h"
CONCURRENT_QUERIES = 6
MAX_SERIES = 30
GRAFANA_VARIABLE = re.compile(r"\$__\w+")
LEGEND_LABEL = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def sections() -> dict[str, list[dict[str, Any]]]:
    """Each dashboard section with its panels that hold queries."""
    with open(DASHBOARD_PATH, encoding="utf-8") as dashboard:
        panels = json.load(dashboard)["panels"]
    found: dict[str, list[dict[str, Any]]] = {}
    current = "Overview"
    for panel in panels:
        if panel.get("type") == "row":
            current = panel.get("title") or current
            found.setdefault(current, []).extend(panel.get("panels") or [])
        else:
            found.setdefault(current, []).append(panel)
    return {
        name: [p for p in members if p.get("targets")]
        for name, members in found.items()
    }


def _queries(panel: dict[str, Any]) -> list[tuple[tuple[str, str], str, str]]:
    """(backend, query, legend) of a panel's queries it can answer."""
    queries = []
    for target in panel.get("targets") or []:
        source = target.get("datasource") or panel.get("datasource") or {}
        backend = DATASOURCES.get(source.get("uid", ""))
        expr = target.get("expr")
        if backend and expr and not target.get("hide"):
            legend = target.get("legendFormat") or ""
            queries.append((backend, GRAFANA_VARIABLE.sub(WINDOW, expr), legend))
    return queries


def _value(value: float, unit: Optional[str]) -> str:
    if unit == "percentunit":
        return f"{value * 100:.0f}%"
    if unit == "percent":
        return f"{value:.0f}%"
    if unit in ("bytes", "decbytes"):
        return f"{value / 2**30:,.1f} GiB"
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text} {unit}" if unit in ("s", "ms") else text


def _series(rows: list[dict[str, Any]], legend: str, unit: Optional[str]) -> list[str]:
    """One line per series, or none when any of them has a label not allowed."""
    lines = []
    for labels, value in prom_vector(rows):
        names = set(labels) - DROPPED_LABELS
        if not names <= ALLOWED_LABELS:
            return []
        if value != value:
            continue
        shown = LEGEND_LABEL.sub(lambda m: str(labels.get(m[1], "")), legend)
        if shown in ("", "__auto"):
            shown = ", ".join(str(labels[name]) for name in sorted(names))
        lines.append(
            f"{shown}: {_value(value, unit)}" if shown else _value(value, unit)
        )
    return lines


async def _panel(
    limit: asyncio.Semaphore, panel: dict[str, Any]
) -> tuple[list[str], bool]:
    """The lines of a panel, and whether every query could be asked."""
    unit = (panel.get("fieldConfig") or {}).get("defaults", {}).get("unit")
    lines: list[str] = []
    answered = True
    for (client, url), query, legend in _queries(panel):
        async with limit:
            rows, problem = await prom_query(shared_client(client), url, query)
        answered = answered and problem is None
        lines += _series(rows, legend, unit)
    return lines, answered


def register(mcp: Any) -> None:
    @mcp.tool()
    async def get_facility_metrics(section: Optional[str] = None) -> str:
        """Current numbers from the public facility dashboard.

        Use this for "how busy is the AF / how many GPUs or sessions / how full
        is storage" questions, rather than sending someone to the dashboard.
        Facility-wide numbers only: per-user and per-pod panels are left out.

        Args:
            section: A dashboard section. Omit it to list the sections.
        """
        available = sections()
        if section is None:
            names = "\n".join(f"- {name}" for name in available)
            return f"Sections of the dashboard:\n{names}\n\nNext: call this with a section."
        wanted = {name.lower(): name for name in available}.get(section.strip().lower())
        if wanted is None:
            raise Failure(
                f"Error: no section {section!r}. Sections: {', '.join(available)}."
            )
        limit = asyncio.Semaphore(CONCURRENT_QUERIES)
        panels = available[wanted]
        results = await asyncio.gather(*(_panel(limit, p) for p in panels))
        asked = [answered for p, (_, answered) in zip(panels, results) if _queries(p)]
        if asked and not any(asked):
            raise UpstreamError("Error: the monitoring system is unreachable.")
        out = [f"**{wanted}**, now (rates and totals over {WINDOW})"]
        for panel, (lines, _) in zip(panels, results):
            shown = lines[:MAX_SERIES]
            if len(shown) == 1:
                out.append(f"- {panel.get('title')}: {shown[0]}")
            elif shown:
                out.append(f"- {panel.get('title')}:")
                out += [f"  - {line}" for line in shown]
                if len(lines) > MAX_SERIES:
                    out.append(f"  - and {len(lines) - MAX_SERIES} more")
        if not all(answered for _, answered in results):
            out.append("\nSome panels could not be read just now, so they are missing.")
        return "\n".join(out)
