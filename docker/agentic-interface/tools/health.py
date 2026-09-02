"""Facility health tool — reads the alerts Prometheus is already evaluating.

Thresholds live in apps/monitoring/prometheus/values.yaml, not here: this tool
reports what is firing, so adding an alert rule improves it automatically and
there is only ever one definition of "unhealthy".

Three rules shape the output:

* Only ``error``/``critical`` alerts make the facility **Degraded**. Warnings
  that still affect users (failed or slow data access, monitor gaps) yield
  **Impaired** — between healthy and broken — so the headline stays honest
  without crying wolf over transient storage blips.
* A signal that could not be collected is ``unknown``, never ``ok`` — the
  data-access checks in particular go blank when they cannot be scheduled,
  which says nothing about the storage itself.
* Every firing alert a user may see is listed under its component; "Normal"
  only covers components with nothing firing at all.

Output is for the calling user: their own quota is reported, no other user's.
"""

import os
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
from context import require_user
from errors import UpstreamError
from shared import prom_query, prom_scalar, quote_label, shared_client

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-server:9090")
EASTERN = ZoneInfo("America/New_York")

# component label -> the heading a user sees. Anything not listed still shows
# up, under its raw component name, so a new alert is never silently dropped.
COMPONENT_TITLES = {
    "access": "Facility access",
    "compute": "Compute capacity",
    "data": "Data access",
    "scale-out": "Scale-out",
    "environment": "Software environment",
    "storage": "User storage",
}
BLOCKING = ("error", "critical")
# The facility is also monitored on dev hardware, which runs no user sessions.
# Those alerts exist for operators; a user asking "is the AF healthy" should
# never see them, and they must not affect the verdict.
HIDDEN_COMPONENTS = {"dev"}
# Per-user quota warnings are already reported as a percent figure below; they
# must not flip the facility headline to Impaired.
SKIP_IMPAIR_PREFIXES = ("AFHomeDir",)


def _et(ts: float) -> str:
    """Absolute Eastern time plus how long ago — both matter when acting."""
    when = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(EASTERN)
    delta = datetime.now(tz=timezone.utc).timestamp() - ts
    hours, minutes = int(delta // 3600), int((delta % 3600) // 60)
    ago = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"
    return f"{when:%H:%M ET, %d %b} ({ago} ago)"


async def _query(
    client: httpx.AsyncClient, expr: str
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Instant query → (rows, problem). A problem means Prometheus could not
    be asked — which is never the same as "nothing is firing"."""
    return await prom_query(client, PROMETHEUS_URL, expr, timeout=10.0)


async def _scalar(client: httpx.AsyncClient, expr: str) -> Optional[float]:
    rows, _ = await _query(client, expr)
    return prom_scalar(rows)


def _node_names(series: list[dict[str, Any]]) -> list[str]:
    """Worker node under test.

    Prefer ``exported_node`` when present: older scrapes collided the monitor
    pod host onto ``node`` and renamed the AF worker to ``exported_node``.
    New scrapes keep the exporter label as ``node`` only.
    """
    names: set[str] = set()
    for s in series:
        m = s["metric"]
        names.add(m.get("exported_node") or m.get("node") or "")
    return sorted(names - {""})


def _describe(alertname: str, series: list[dict[str, Any]]) -> str:
    """One line per alert, in facility terms rather than metric terms."""
    count = len(series)
    mounts = sorted({s["metric"].get("mount_name", "") for s in series} - {""})
    nodes = _node_names(series)
    where = f"{len(nodes)} node{'s' if len(nodes) != 1 else ''}"
    widespread = len(nodes) > 2
    scope_system = (
        "across most of the facility — likely a storage-system or network "
        "issue rather than one machine"
        if widespread
        else "on a single machine"
    )
    mount_list = ", ".join(mounts) or "a data path"

    if alertname == "AFMountInvalid":
        return f"{mount_list} failing on {where}, {scope_system}"
    if alertname == "AFMountSlow":
        detail = "across most of the facility" if widespread else "on a single machine"
        return (
            f"{mount_list} is slow on {where}, {detail} — "
            "reads still work, but analyses on affected nodes will crawl"
        )
    if alertname == "AFMountHealthUnknown":
        return (
            f"data-access checks have not reported on {len(nodes)} Ready node"
            f"{'s' if len(nodes) != 1 else ''} — state is unknown there "
            "(not the same as a failed check, and offline nodes are omitted)"
        )
    if alertname == "AFProdNodesNotReady":
        listed = ", ".join(nodes[:8])
        more = f" (+{len(nodes) - 8} more)" if len(nodes) > 8 else ""
        return (
            f"{len(nodes)} worker node{'s' if len(nodes) != 1 else ''} NotReady"
            f"{f' ({listed}{more})' if listed else ''} — "
            "sessions and Dask workers cannot run on them"
        )
    if alertname == "AFHubDown":
        return "the hub is not responding; sessions cannot be started or reached"
    if alertname == "AFSpawnFailures":
        return "sessions have been failing to start"
    if alertname == "AFSessionStuckPending":
        return f"{count} session{'s' if count != 1 else ''} stuck waiting to start"
    if alertname == "AFDaskGatewayDown":
        return "a Dask gateway is unavailable; new clusters may not start"
    if alertname == "AFGlobalEnvDaemonStale":
        return (
            "the global pixi sync loop looks wedged — heartbeat is stale, "
            "rebuilds may be hung"
        )
    if alertname == "AFGlobalEnvSyncDown":
        return "global pixi sync metrics are missing; the reconciler may be down"
    if alertname.startswith("AFGlobalEnv"):
        return "the shared pixi environment is not in sync with the AF repository"
    if alertname == "AFNodeMonitorStale":
        return (
            "data-access health checks have not completed recently — "
            "mount state may be stale"
        )
    return alertname


def register(mcp: Any) -> None:
    @mcp.tool()
    async def get_facility_health() -> str:
        """Report whether the Purdue Analysis Facility is healthy right now.

        Summarises the alerts Prometheus is evaluating across facility access,
        compute capacity, data access, scale-out and the software environment,
        plus the calling user's own storage quota, plus what has fired in the
        last 6 hours.

        Use this for "is the AF healthy / is something broken / why is my
        session slow" questions before investigating anything specific.
        """
        user = require_user()
        username = user["username"]

        quoted = quote_label(username)

        client = shared_client("prometheus")
        firing, problem = await _query(client, 'ALERTS{alertstate="firing"}')
        if problem:
            # An empty alert list from a Prometheus that answered is good
            # news; no answer at all is not news of any kind.
            raise UpstreamError(
                f"Error: the monitoring system {problem}, so I cannot tell you "
                "whether the facility is healthy. This is a monitoring problem, "
                "not necessarily a facility problem — try your work and see."
            )
        # ALERTS_FOR_STATE carries the moment each alert started, which is
        # what a user needs in order to correlate it with their own trouble.
        since, _ = await _query(client, "ALERTS_FOR_STATE")
        # An alert that came and went in the window is a different situation
        # from something steadily broken.
        recent, _ = await _query(
            client,
            "count by (alertname) (changes(ALERTS_FOR_STATE[6h]) > 0)",
        )
        home_util = await _scalar(client, f'af_home_dir_util{{username="{quoted}"}}')
        home_size = await _scalar(client, f'af_home_dir_size_kb{{username="{quoted}"}}')

        # group firing alerts by component, then by alert name
        by_component: dict[str, dict[str, list[dict[str, Any]]]] = {}
        severities: dict[str, str] = {}
        # every firing name, including ones hidden below — used to tell a
        # resolved alert apart from one that is simply not shown to this user
        firing_names: set[str] = {s["metric"].get("alertname", "") for s in firing}
        for series in firing:
            labels = series["metric"]
            # Never surface another user's alert. Checked on the label rather
            # than the component so an alert added later cannot leak by being
            # filed under a component this tool does not know about.
            owner = labels.get("username")
            if owner and owner != username:
                continue
            if labels.get("component") in HIDDEN_COMPONENTS:
                continue
            if labels.get("node_pool") == "dev":
                continue
            component = labels.get("component", "other")
            name = labels.get("alertname", "unknown")
            by_component.setdefault(component, {}).setdefault(name, []).append(series)
            severities[name] = labels.get("severity", "warning")

        started: dict[str, float] = {}
        for series in since:
            name = series["metric"].get("alertname", "")
            try:
                ts = float(series["value"][1])
            except (KeyError, IndexError, ValueError):
                continue
            if name and (name not in started or ts < started[name]):
                started[name] = ts

        blocking = [n for n, sev in severities.items() if sev in BLOCKING]
        unknown = "AFMountHealthUnknown" in severities
        # Warnings users feel (slow mounts, stale checks) — not quiet quota
        # notices, and not the unknown case which already has its own verdict.
        impaired = [
            n
            for n, sev in severities.items()
            if sev not in BLOCKING
            and n != "AFMountHealthUnknown"
            and not n.startswith(SKIP_IMPAIR_PREFIXES)
        ]

        if blocking:
            verdict = "**Degraded**"
        elif unknown:
            verdict = "**Partly unknown**"
        elif impaired:
            verdict = "**Impaired**"
        else:
            verdict = "**Healthy**"

        lines = [f"{verdict} — Purdue Analysis Facility\n"]

        if not blocking and not unknown and not impaired:
            lines.append("Nothing is failing across the facility.")

        for component, alerts in sorted(by_component.items()):
            title = COMPONENT_TITLES.get(component, component)
            if component == "storage":
                continue  # per-user, reported separately below
            for name, matched in sorted(alerts.items()):
                mark = "!" if severities[name] in BLOCKING else "-"
                when = f" Since {_et(started[name])}." if name in started else ""
                lines.append(f"{mark} **{title}**: {_describe(name, matched)}.{when}")

        clean = [
            title
            for component, title in sorted(COMPONENT_TITLES.items())
            if component not in by_component
            and component != "storage"
            and component not in HIDDEN_COMPONENTS
        ]
        if clean:
            lines.append(f"\nNormal: {', '.join(clean).lower()}.")

        if home_util is not None:
            quota_gb = (home_size or 0) / 1024 / 1024
            note = (
                "  Approaching the limit — a full home directory stops sessions "
                "from starting."
                if home_util >= 0.9
                else ""
            )
            lines.append(
                f"\nYour home directory is at {home_util * 100:.0f}% "
                f"of its {quota_gb:.0f} GB quota.{note}"
            )
        else:
            lines.append(
                "\nNo reading of your home directory quota right now — it is "
                "reported by your running session, so this is expected while no "
                "session is running (query_storage_usage has the details)."
            )

        # "came and went" means resolved — an alert still firing is reported
        # above, and counting it again here reads as extra trouble.
        settled = [
            r["metric"].get("alertname")
            for r in recent
            if r["metric"].get("alertname") not in firing_names
        ]
        if settled:
            lines.append(
                f"\nIn the last 6 hours, {len(settled)} issue"
                f"{'s' if len(settled) != 1 else ''} came and went."
            )
        elif blocking:
            lines.append(
                "\nThis has been steady for the last 6 hours, not intermittent."
            )

        return "\n".join(lines) + "\n"
