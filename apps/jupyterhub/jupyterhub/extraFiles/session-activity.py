"""Export the hub's own idea of session activity to /hub/metrics.

Prometheus already scrapes /hub/metrics (the `jupyterhub` job), so a collector
on the default registry is all this needs — no service, token or scrape config.

The dashboard's "Last active" column used to come from af_home_dir_last_accessed,
the st_atime of the user's home directory. That measures the mount, not the
session: on this NFS it read years stale for most users, and identical for
everyone at once whenever a sweep touched /home. This exports what JupyterHub
itself calls activity — the signal the cullers act on.
"""

import datetime
import logging
from collections.abc import Iterator
from typing import Any

from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily

# `c` is the traitlets config object JupyterHub injects into this file's
# globals at exec time. A bare annotation declares its type for static
# checkers without creating (or shadowing) the runtime binding.
c: Any

# kubespawner renders pod names from this; the Grafana table joins on `pod`.
POD_NAME_TEMPLATE: str = c.KubeSpawner.pod_name_template or "purdue-af-{userid}"
LABELS = ["username", "servername", "pod"]

log = logging.getLogger("session-activity")


def to_timestamp(value: datetime.datetime | None) -> float | None:
    """Unix time from a JupyterHub ORM column, which stores naive UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.timestamp()


def pod_name(username: str, userid: int, servername: str) -> str:
    return POD_NAME_TEMPLATE.format(
        userid=userid, username=username, servername=servername
    )


def running_spawners() -> Any:
    """ORM spawners that have a server attached — the hub's own "running"."""
    from jupyterhub import orm
    from jupyterhub.app import JupyterHub

    db = JupyterHub.instance().db
    return db.query(orm.Spawner).filter(orm.Spawner.server_id.isnot(None))


def sessions() -> list[tuple[list[str], float, float]]:
    """(labels, last activity, start time) for every running session."""
    rows = []
    for spawner in running_spawners():
        user = spawner.user
        if user is None:
            continue
        servername = spawner.name or ""
        started = to_timestamp(spawner.started)
        # a session that has reported nothing yet is as active as its spawn
        last = to_timestamp(spawner.last_activity) or started
        if last is None or started is None:
            continue
        labels = [user.name, servername, pod_name(user.name, user.id, servername)]
        rows.append((labels, last, started))
    return rows


class SessionActivityCollector:
    """Reads the ORM at scrape time, so stopped sessions drop out on their own."""

    def collect(self) -> Iterator[GaugeMetricFamily]:
        activity = GaugeMetricFamily(
            "af_session_last_activity_seconds",
            "Unix time of the last activity JupyterHub recorded for a session",
            labels=LABELS,
        )
        started = GaugeMetricFamily(
            "af_session_started_seconds",
            "Unix time a session was spawned",
            labels=LABELS,
        )
        try:
            rows = sessions()
        except Exception:
            # /hub/metrics serves every JupyterHub metric from this registry;
            # raising here would take all of them down with it.
            log.exception("session activity collection failed")
            rows = []
        for labels, last, start in rows:
            activity.add_metric(labels, last)
            started.add_metric(labels, start)
        yield activity
        yield started


REGISTRY.register(SessionActivityCollector())
