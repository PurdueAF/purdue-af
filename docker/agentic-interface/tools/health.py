"""Facility health tool — reads the alerts Prometheus is already evaluating.

Thresholds and wording live in apps/monitoring/prometheus/values.yaml: each
rule's ``annotations.summary`` is what a user sees.

* Only ``error``/``critical`` alerts make the facility **Degraded**; warnings
  yield **Impaired**.
* A signal that could not be collected is ``unknown``, never ``ok``, and a read
  that failed draws no conclusion.
* Every firing alert a user may see is listed under its component; "Normal"
  only covers components with nothing firing at all.
* A rule labelled ``audience: operators`` is not shown to users.
* A user's own quota alert is about them, not the facility: it leaves the
  headline alone and shows in the quota line. No other user's is shown.

Rows of the public dashboard's Components panel that no alert covers count too,
so that this tool and the dashboard cannot disagree: a row at zero is down and
makes the facility **Degraded**, a row in between makes it **Impaired**.
"""

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional
from zoneinfo import ZoneInfo

import httpx
from config import PROMETHEUS_NAME, PROMETHEUS_URL
from context import require_user
from errors import http_error, json_body, malformed_response, unreachable
from mcp.server.mcpserver import MCPServer
from shared import prom_samples, prom_value, quote_label, shared_client

from tools.metrics import component_problems
from tools.storage import dir_reading_query

EASTERN = ZoneInfo("America/New_York")

# component label -> the heading a user sees; others show under their raw name.
COMPONENT_TITLES = {
    "access": "Facility access",
    "compute": "Compute capacity",
    "data": "Data access",
    "scale-out": "Scale-out",
    "environment": "Software environment",
    "storage": "User storage",
    "observability": "Logs and monitoring",
}
BLOCKING = ("error", "critical")
HEALTHY = "**Healthy**"
DEGRADED = "**Degraded**"
# Dev hardware runs no sessions; a failed release leaves the workloads running.
HIDDEN_COMPONENTS = {"dev", "delivery"}
# The value of a rule's ``audience`` label that keeps it from users.
OPERATORS = "operators"
_FRACTION_RE = re.compile(r"\.(\d{6})\d+")
# username="": on the label, so that no later alert about one person can leak.
_HISTORY_QUERY = (
    'count by (alertname) (count_over_time(ALERTS{alertstate="firing",'
    f'component!~"{"|".join(sorted(HIDDEN_COMPONENTS))}",audience!="{OPERATORS}",'
    'username="",node_pool!="dev"}[6h]))'
)
_UNKNOWN = (
    "I cannot tell you whether the facility is healthy. This is a monitoring "
    "problem, not necessarily a facility problem — try your work and see."
)


def _et(ts: float) -> str:
    """Absolute Eastern time plus how long ago — both matter when acting."""
    when = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(EASTERN)
    delta = datetime.now(tz=timezone.utc).timestamp() - ts
    hours, minutes = int(delta // 3600), int((delta % 3600) // 60)
    ago = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"
    return f"{when:%H:%M ET, %d %b} ({ago} ago)"


def _active_at(alert: dict[str, Any]) -> Optional[float]:
    """Prometheus reports activeAt as RFC 3339 with nanoseconds."""
    raw = alert.get("activeAt")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(_FRACTION_RE.sub(r".\1", raw)).timestamp()
    except ValueError:
        return None


async def _firing_alerts(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Every firing alert with its labels and rendered annotations."""
    try:
        resp = await client.get(f"{PROMETHEUS_URL}/api/v1/alerts", timeout=10.0)
    except httpx.RequestError as exc:
        raise unreachable(PROMETHEUS_NAME, exc, next_step=_UNKNOWN)
    if resp.status_code != 200:
        raise http_error(
            PROMETHEUS_NAME,
            resp,
            action="read the firing alerts",
            next_step=_UNKNOWN,
        )
    payload = json_body(resp)
    data = payload.get("data") if isinstance(payload, dict) else None
    alerts = data.get("alerts") if isinstance(data, dict) else None
    if not isinstance(alerts, list):
        raise malformed_response(
            PROMETHEUS_NAME, resp, "an alert list", next_step=_UNKNOWN
        )
    return [a for a in alerts if isinstance(a, dict) and a.get("state") == "firing"]


def _nodes(alerts: list[dict[str, Any]]) -> set[str]:
    """Nodes an alert group covers (``exported_node`` when a scrape collided
    the monitor pod's host onto ``node``)."""
    names = {
        a["labels"].get("exported_node") or a["labels"].get("node") or ""
        for a in alerts
    }
    return names - {""}


class _Alerts(NamedTuple):
    """The firing alerts as this user sees them."""

    by_component: dict[str, dict[str, list[dict[str, Any]]]]
    severities: dict[str, str]
    # Every firing name, hidden ones too: those have not resolved.
    names: set[str]
    own_quota_alert: bool = False


def _visible_alerts(firing: list[dict[str, Any]], username: str) -> _Alerts:
    """Group what this user may see by component, then by alert name."""
    seen = _Alerts({}, {}, set())
    own_quota_alert = False
    for alert in firing:
        labels = alert.get("labels") or {}
        alert["labels"] = labels
        name = labels.get("alertname", "unknown")
        seen.names.add(name)
        # On the label, so a later alert cannot leak under an unknown component.
        owner = labels.get("username")
        if owner and owner != username:
            continue
        if labels.get("component") in HIDDEN_COMPONENTS:
            continue
        if labels.get("audience") == OPERATORS or labels.get("node_pool") == "dev":
            continue
        component = labels.get("component", "other")
        if component == "storage" and owner:
            own_quota_alert = True  # one person's quota, not the facility's state
            continue
        seen.by_component.setdefault(component, {}).setdefault(name, []).append(alert)
        seen.severities[name] = labels.get("severity", "warning")
    return seen._replace(own_quota_alert=own_quota_alert)


def _uncovered_rows(
    problems: dict[str, float], by_component: dict[str, dict[str, list[dict[str, Any]]]]
) -> dict[str, float]:
    """The Components rows in trouble that no shown alert already mentions."""
    summaries = " ".join(
        str((alert.get("annotations") or {}).get("summary"))
        for alerts in by_component.values()
        for matched in alerts.values()
        for alert in matched
    )
    return {name: value for name, value in problems.items() if name not in summaries}


def _verdict(
    severities: dict[str, str], rows: dict[str, float], unread: list[str]
) -> str:
    down = any(value <= 0 for value in rows.values())
    partial = any(value > 0 for value in rows.values())
    # Warnings users feel; the unknown case has its own verdict.
    impaired = any(
        sev not in BLOCKING and name != "AFMountHealthUnknown"
        for name, sev in severities.items()
    )
    if down or any(sev in BLOCKING for sev in severities.values()):
        return DEGRADED
    if unread or "AFMountHealthUnknown" in severities:
        return "**Partly unknown**"
    if impaired or partial:
        return "**Impaired**"
    return HEALTHY


def _row_lines(rows: dict[str, float], unread: list[str]) -> list[str]:
    lines = [
        f"! **{name}** is down on the facility dashboard."
        for name, value in rows.items()
        if value <= 0
    ]
    lines += [
        f"- **{name}** is partly failing on the facility dashboard: some of its "
        "checks fail."
        for name, value in rows.items()
        if value > 0
    ]
    if unread:
        lines.append(f"- Could not read: {', '.join(unread)}.")
    return lines


def _alert_lines(alerts: _Alerts) -> list[str]:
    lines = []
    for component, named in sorted(alerts.by_component.items()):
        title = COMPONENT_TITLES.get(component, component)
        for name, matched in sorted(named.items()):
            mark = "!" if alerts.severities[name] in BLOCKING else "-"
            # The first instance's rendered summary; the rest are counted.
            summary = (matched[0].get("annotations") or {}).get("summary") or name
            scale = ""
            if len(matched) > 1:
                nodes = _nodes(matched)
                scale = f" (and {len(matched) - 1} more"
                scale += f", {len(nodes)} nodes affected)" if nodes else ")"
            starts = [t for t in map(_active_at, matched) if t is not None]
            when = f" Since {_et(min(starts))}." if starts else ""
            lines.append(f"{mark} **{title}**: {summary}{scale}.{when}")
    return lines


def _normal_line(by_component: dict[str, Any]) -> str:
    clean = [
        title
        for component, title in sorted(COMPONENT_TITLES.items())
        if component not in by_component
        and component != "storage"
        and component not in HIDDEN_COMPONENTS
    ]
    return f"\nNormal: {', '.join(clean).lower()}." if clean else ""


def _quota_line(
    util: Optional[float],
    size_kb: Optional[float],
    problem: Optional[str],
    near_limit: bool,
) -> str:
    note = (
        "  Approaching the limit — a full home directory stops sessions from starting."
        if near_limit
        else ""
    )
    if util is not None:
        quota = "quota" if size_kb is None else f"{size_kb / 1024 / 1024:.0f} GB quota"
        return f"\nYour home directory is at {util * 100:.0f}% of its {quota}.{note}"
    if problem:
        return (
            "\nYour home directory quota could not be read just now "
            f"(query_storage_usage tries again).{note}"
        )
    return (
        "\nNo reading of your home directory quota right now — it is "
        "reported by your running session, so this is expected while no "
        f"session is running (query_storage_usage has the details).{note}"
    )


def _history_line(settled: list[str], problem: Optional[str], blocking: bool) -> str:
    if problem:
        return "\nThe last 6 hours of alert history could not be read."
    if settled:
        return (
            f"\nIn the last 6 hours, {len(settled)} issue"
            f"{'s' if len(settled) != 1 else ''} came and went."
        )
    if blocking:
        return "\nThis has been steady for the last 6 hours, not intermittent."
    return ""


def register(mcp: MCPServer) -> None:
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
        username = require_user()["username"]
        selector = f'username="{quote_label(username)}"'

        client = shared_client("prometheus")
        firing = await _firing_alerts(client)
        (
            (recent, history_problem),
            (home_util, util_problem),
            (home_size, _),
        ) = await asyncio.gather(
            prom_samples(client, PROMETHEUS_URL, _HISTORY_QUERY, timeout=10.0),
            prom_value(
                client, PROMETHEUS_URL, dir_reading_query("home", "util", selector)
            ),
            prom_value(
                client,
                PROMETHEUS_URL,
                dir_reading_query("home", "size_kb", selector),
            ),
        )

        alerts = _visible_alerts(firing, username)
        problems, unread = await component_problems()
        rows = _uncovered_rows(problems, alerts.by_component)
        verdict = _verdict(alerts.severities, rows, unread)

        lines = [f"{verdict} — Purdue Analysis Facility\n"]
        if verdict == HEALTHY:
            lines.append("Nothing is failing across the facility.")
        lines += _row_lines(rows, unread)
        lines += _alert_lines(alerts)
        if not rows and not unread:
            lines.append(_normal_line(alerts.by_component))
        lines.append(
            _quota_line(home_util, home_size, util_problem, alerts.own_quota_alert)
        )
        # "came and went" means resolved: a firing alert is reported above.
        settled = [
            str(labels.get("alertname"))
            for labels, _ in recent
            if labels.get("alertname") not in alerts.names
        ]
        blocking = any(sev in BLOCKING for sev in alerts.severities.values())
        lines.append(_history_line(settled, history_problem, blocking))
        return "\n".join(line for line in lines if line) + "\n"
