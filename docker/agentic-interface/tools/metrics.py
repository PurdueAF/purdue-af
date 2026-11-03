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
from mcp.server.mcpserver import MCPServer
from shared import prom_query, prom_samples, prom_vector, shared_client

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
COMPONENTS_PANEL = "Components"
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


async def component_problems() -> tuple[dict[str, float], list[str]]:
    """The dashboard's Components rows that are not OK, by name, with the
    fraction each one reads, and the rows that could not be read. Both are
    empty when the dashboard is not mounted."""
    try:
        panels = [p for members in sections().values() for p in members]
    except OSError:
        return {}, []
    problems: dict[str, float] = {}
    unread: list[str] = []
    for panel in panels:
        if panel.get("title") != COMPONENTS_PANEL:
            continue
        for (client, url), query, legend in _queries(panel):
            samples, problem = await prom_samples(shared_client(client), url, query)
            if problem:
                unread.append(legend)
            for _, value in samples:
                if value < 1:
                    problems[legend] = value
    return problems, unread


def _mapped(value: float, mappings: list[dict[str, Any]]) -> Optional[str]:
    """The word a panel's value mappings give a value, as Grafana shows it."""
    for mapping in mappings:
        options = mapping.get("options") or {}
        if mapping.get("type") == "value":
            key = f"{value:g}"
            text = (options.get(key) or {}).get("text")
        elif mapping.get("type") == "range":
            low, high = options.get("from"), options.get("to")
            inside = (low is None or value >= low) and (high is None or value <= high)
            text = (options.get("result") or {}).get("text") if inside else None
        else:
            text = None
        if text:
            return str(text)
    return None


def _named_mappings(panel: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Value mappings a panel gives single series by name, which override its own."""
    named: dict[str, list[dict[str, Any]]] = {}
    overrides = (panel.get("fieldConfig") or {}).get("overrides") or []
    for override in overrides:
        matcher = override.get("matcher") or {}
        if matcher.get("id") != "byName":
            continue
        for prop in override.get("properties") or []:
            if prop.get("id") == "mappings":
                named.setdefault(str(matcher.get("options")), []).extend(
                    prop.get("value") or []
                )
    return named


def _value(
    value: float, unit: Optional[str], mappings: Optional[list[dict[str, Any]]] = None
) -> str:
    word = _mapped(value, mappings or [])
    if word:
        return word
    if unit == "percentunit":
        return f"{value * 100:.0f}%"
    if unit == "percent":
        return f"{value:.0f}%"
    if unit in ("bytes", "decbytes"):
        return f"{value / 2**30:,.1f} GiB"
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text} {unit}" if unit in ("s", "ms") else text


def _series(
    rows: list[dict[str, Any]],
    legend: str,
    unit: Optional[str],
    mappings: Optional[list[dict[str, Any]]] = None,
    named: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> list[str]:
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
        own = (named or {}).get(shown, [])
        text = _value(value, unit, own + (mappings or []))
        lines.append(f"{shown}: {text}" if shown else text)
    return lines


async def _panel(
    limit: asyncio.Semaphore, panel: dict[str, Any]
) -> tuple[list[str], bool]:
    """The lines of a panel, and whether every query could be asked."""
    defaults = (panel.get("fieldConfig") or {}).get("defaults", {})
    unit, mappings = defaults.get("unit"), defaults.get("mappings")
    named = _named_mappings(panel)
    lines: list[str] = []
    answered = True
    for (client, url), query, legend in _queries(panel):
        async with limit:
            rows, problem = await prom_query(shared_client(client), url, query)
        answered = answered and problem is None
        lines += _series(rows, legend, unit, mappings, named)
    return lines, answered


def register(mcp: MCPServer) -> None:
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
