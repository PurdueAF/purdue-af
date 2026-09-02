"""Facility health tool — reads the alerts Prometheus is already evaluating.

Thresholds AND wording live in apps/monitoring/prometheus/values.yaml, not
here: each rule's ``annotations.summary`` is what a user sees, so adding or
rewording an alert improves this tool with no code change, and there is only
ever one definition of "unhealthy" and one sentence describing it.

Three rules shape the output:

* Only ``error``/``critical`` alerts make the facility **Degraded**. Warnings
  that still affect users (failed or slow data access, monitor gaps) yield
  **Impaired** — between healthy and broken — so the headline stays honest
  without crying wolf over transient storage blips.
* A signal that could not be collected is ``unknown``, never ``ok``.
* Every firing alert a user may see is listed under its component; "Normal"
  only covers components with nothing firing at all.

Output is for the calling user: their own quota is reported, no other user's.
"""

import re
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
from config import PROMETHEUS_URL
from context import require_user
from errors import UpstreamError, describe_exception, json_body, response_detail
from shared import prom_query, prom_scalar, quote_label, shared_client

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
_FRACTION_RE = re.compile(r"\.(\d{6})\d+")


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
        raise UpstreamError(
            f"Error: the monitoring system is unreachable — {describe_exception(exc)}, "
            "so I cannot tell you whether the facility is healthy. This is a "
            "monitoring problem, not necessarily a facility problem — try your "
            "work and see."
        )
    if resp.status_code != 200:
        detail = response_detail(resp, limit=160)
        raise UpstreamError(
            f"Error: the monitoring system returned HTTP {resp.status_code}"
            + (f" — {detail}" if detail else "")
            + ", so I cannot tell you whether the facility is healthy. This is a "
            "monitoring problem, not necessarily a facility problem — try your "
            "work and see."
        )
    payload = json_body(resp)
    data = payload.get("data") if isinstance(payload, dict) else None
    alerts = data.get("alerts") if isinstance(data, dict) else None
    if not isinstance(alerts, list):
        raise UpstreamError(
            "Error: the monitoring system returned HTTP 200 but no alert list, so "
            "I cannot tell you whether the facility is healthy."
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
        firing = await _firing_alerts(client)
        # An alert that came and went in the window is a different situation
        # from something steadily broken.
        recent, _ = await prom_query(
            client,
            PROMETHEUS_URL,
            "count by (alertname) (changes(ALERTS_FOR_STATE[6h]) > 0)",
            timeout=10.0,
        )
        home_rows, _ = await prom_query(
            client, PROMETHEUS_URL, f'af_home_dir_util{{username="{quoted}"}}'
        )
        size_rows, _ = await prom_query(
            client, PROMETHEUS_URL, f'af_home_dir_size_kb{{username="{quoted}"}}'
        )
        home_util = prom_scalar(home_rows)
        home_size = prom_scalar(size_rows)

        # group firing alerts by component, then by alert name
        by_component: dict[str, dict[str, list[dict[str, Any]]]] = {}
        severities: dict[str, str] = {}
        # every firing name, including ones hidden below — used to tell a
        # resolved alert apart from one that is simply not shown to this user
        firing_names: set[str] = set()
        for alert in firing:
            labels = alert.get("labels") or {}
            alert["labels"] = labels
            name = labels.get("alertname", "unknown")
            firing_names.add(name)
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
            by_component.setdefault(component, {}).setdefault(name, []).append(alert)
            severities[name] = labels.get("severity", "warning")

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
                # The rule's own summary, rendered by Prometheus for the first
                # instance; the rest are counted so scale is never lost.
                summary = (matched[0].get("annotations") or {}).get("summary") or name
                scale = ""
                if len(matched) > 1:
                    nodes = _nodes(matched)
                    scale = f" (and {len(matched) - 1} more"
                    scale += f", {len(nodes)} nodes affected)" if nodes else ")"
                starts = [t for t in map(_active_at, matched) if t is not None]
                when = f" Since {_et(min(starts))}." if starts else ""
                lines.append(f"{mark} **{title}**: {summary}{scale}.{when}")

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
            if r.get("metric", {}).get("alertname") not in firing_names
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
