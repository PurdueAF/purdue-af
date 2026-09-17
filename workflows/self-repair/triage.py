"""Everything in the self-repair workflow that is not a Flyte task: the Loki
query, error fingerprints, username redaction, the agent's verdict and the
GitHub calls. No flyte import, so the tests run without the SDK."""

import asyncio
import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable

ERROR_PATTERN = r"(?i)\b(error|exception|traceback|fatal|panic)\b"

# Pod-name prefixes of what Flux deploys from this repository (deploy/*/
# kustomization.yaml), i.e. what a change here can fix. Whatever else lives
# in the namespace without a manifest here (gen*, etcd, eos-fuse, the one-off
# kaniko builds) is not read at all.
WATCHED_WORKLOADS = (
    # apps/jupyterhub
    "hub",
    "proxy",
    "user-scheduler",
    "continuous-image-puller",
    "hook-image",
    "jupyterhub-ssh",
    "jupyterhub-sftp",
    "jupyterhub-database-backup",
    "af-x509-secrets",
    "af-userlist-sync",
    # apps/af-utils
    "af-users-graph",
    "pixi-global-sync",
    # apps/dask-gateway
    "api-dask-gateway",
    "controller-dask-gateway",
    "traefik-dask-gateway",
    # apps/monitoring
    "alloy",
    "loki",
    "tempo",
    "pyroscope",
    "prometheus",
    "grafana",
    "af-node-monitor",
    "af-node-probe",
    "af-pod-monitor",
    # apps/agentic-interface, apps/flyte. Not this workflow's own pods: their
    # logs quote every error they analyze, and would feed it back next tick.
    "agentic-interface",
    "flyte",
    # apps/sonic, apps/ray
    "supersonic",
    "sonic-ray",
    "kuberay-operator",
    # apps/servicex
    "servicex",
    # apps/interlink
    "interlink",
)

# Pods this repository configures but which run user code: sessions
# (purdue-af-<id>) and user Dask clusters. The image, its start hooks, the
# pixi environments and the gateway's worker config are fixable here; a
# notebook cell is not. Read, but ranked after everything above so they only
# use analysis budget the infrastructure did not.
USER_WORKLOADS = ("purdue-af", "dask-scheduler", "dask-worker")

# Deployed from here, but not to be debugged by this workflow for now. Kept
# as a separate list so WATCHED_WORKLOADS stays the inventory of what the
# repository deploys; every entry must appear there.
IGNORED_WORKLOADS = (
    # apps/sonic and apps/ray: the whole SONIC stack
    "supersonic",
    "sonic-ray",
    "kuberay-operator",
    # apps/interlink
    "interlink",
)
ALL_WORKLOADS = tuple(
    prefix
    for prefix in WATCHED_WORKLOADS + USER_WORKLOADS
    if prefix not in IGNORED_WORKLOADS
)
MAX_SAMPLES = 8
MAX_LINE = 400
MESSAGE_CHARS = 240

# Names of OTHER pods inside a message (a tailer target, a "pod not found",
# a scheduler line) must not split one incident per pod. Kubernetes builds
# its random suffixes from this 27-character alphabet: no vowels, no 0 or 1.
_K8S_SUFFIX = "[bcdfghjklmnpqrstvwxz2456789]{5}"
_POD_NAMES = [
    (re.compile(r"-[0-9a-f]{32}(?![0-9a-f])"), "-<id>"),  # dask-gateway cluster id
    (
        re.compile(rf"-[0-9a-f]{{8,10}}-{_K8S_SUFFIX}(?![a-z0-9])"),
        "-<pod>",
    ),  # ReplicaSet pod
    (re.compile(rf"-{_K8S_SUFFIX}(?![a-z0-9])"), "-<pod>"),  # DaemonSet/Job pod
]

# Order matters: the specific shapes first, the bare number last.
_NORMALIZE = [
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
        ),
        "<ts>",
    ),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<time>"),
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
        "<uuid>",
    ),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<addr>"),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "#"),
    (re.compile(r"\s+"), " "),
]

# Usernames appear in pod names, home paths and depot paths; none of that
# belongs in a PR. Loki labels other than pod/container are never read.
_REDACT = [
    (re.compile(r"\bjupyter-[A-Za-z0-9._-]+"), "jupyter-<user>"),
    (re.compile(r"/home/[^/\s'\"]+"), "/home/<user>"),
    (re.compile(r"/depot/cms/users/[^/\s'\"]+"), "/depot/cms/users/<user>"),
    (re.compile(r"/work/users/[^/\s'\"]+"), "/work/users/<user>"),
    (re.compile(r"\b(user(?:name)?)=\S+"), r"\1=<user>"),
]

_POD_SUFFIXES = [
    re.compile(r"-[0-9a-f]{32}$"),  # dask-gateway cluster id
    re.compile(r"-[0-9a-f]{8,10}$"),  # ReplicaSet hash
    re.compile(r"-[a-z0-9]{5}$"),  # pod hash
    re.compile(r"-\d+$"),  # StatefulSet ordinal, CronJob timestamp
]


@dataclass(frozen=True)
class IncidentKey:
    """What identifies a recurring error. Every field is part of the cache
    key of `analyze`, so nothing here may vary between occurrences."""

    fingerprint: str
    container: str
    workload: str
    message: str


@dataclass
class Evidence:
    samples: list[str]
    count: int
    pods: int
    first_seen: str
    last_seen: str
    # where the first sample came from, so the analysis can fetch the lines
    # around it: a traceback's exception is on the lines after its header.
    first_pod: str = ""
    first_ts: str = ""


@dataclass
class Incident:
    key: IncidentKey
    evidence: Evidence


@dataclass
class Verdict:
    fixable: bool
    confidence: float = 0.0
    component: str = ""
    title: str = ""
    reason: str = ""
    plan: str = ""


@dataclass
class Summary:
    window_start: str
    window_end: str
    lines: int
    incidents: int
    fixable: int
    pull_requests: list[str] = field(default_factory=list)
    model: str = ""
    outcome: str = "ok"  # one of OUTCOMES


# ── Loki ───────────────────────────────────────────────────────────────────────


_PREFIX = re.compile(r"[a-z0-9-]+")


def pod_regex(prefixes: tuple[str, ...] = ALL_WORKLOADS) -> str:
    """A LogQL `pod=~` value matching `<prefix>` or `<prefix>-<anything>`.
    Loki anchors the regex. No escaping: LogQL parses the string literal
    before the regex and rejects a backslash-dash as an invalid char escape,
    so the
    prefixes must be plain `[a-z0-9-]`, which they are checked to be."""
    for prefix in prefixes:
        if not _PREFIX.fullmatch(prefix):
            raise ValueError(f"pod prefix {prefix!r} is not plain [a-z0-9-]")
    return "(" + "|".join(prefixes) + ")(-.*)?"


def watched(pod: str, prefixes: tuple[str, ...] = ALL_WORKLOADS) -> bool:
    return re.fullmatch(pod_regex(prefixes), pod) is not None


def loki_url(
    base: str,
    namespace: str,
    start: datetime,
    end: datetime,
    limit: int,
    prefixes: tuple[str, ...] = ALL_WORKLOADS,
) -> str:
    query = '{namespace="%s", pod=~"%s"} |~ "%s"' % (
        namespace,
        pod_regex(prefixes),
        ERROR_PATTERN.replace("\\", "\\\\"),
    )
    params = {
        "query": query,
        "start": str(int(start.timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "limit": str(limit),
        "direction": "backward",
    }
    return f"{base}/loki/api/v1/query_range?{urllib.parse.urlencode(params)}"


def query_loki(
    base: str,
    namespace: str,
    start: datetime,
    end: datetime,
    limit: int = 5000,
    prefixes: tuple[str, ...] = ALL_WORKLOADS,
) -> list[dict[str, str]]:
    """Every matching line in the window as {pod, container, ts, line}."""
    with urllib.request.urlopen(
        loki_url(base, namespace, start, end, limit, prefixes), timeout=60
    ) as resp:
        payload = json.load(resp)
    return parse_loki(payload)


def parse_loki(payload: dict[str, Any]) -> list[dict[str, str]]:
    lines = []
    for stream in payload.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        pod = labels.get("pod", "unknown")
        container = labels.get("container", "unknown")
        for ts_ns, line in stream.get("values", []):
            ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
            lines.append(
                {"pod": pod, "container": container, "ts": ts.isoformat(), "line": line}
            )
    lines.sort(key=lambda entry: entry["ts"])
    return lines


# ── Fingerprints ───────────────────────────────────────────────────────────────


def workload_of(pod: str) -> str:
    if pod.startswith("jupyter-"):
        return "jupyter-*"
    previous = None
    while previous != pod:
        previous = pod
        for suffix in _POD_SUFFIXES:
            pod = suffix.sub("", pod)
    return pod


def redact(text: str) -> str:
    for pattern, replacement in _REDACT:
        text = pattern.sub(replacement, text)
    return text


_LOGFMT_LINE = re.compile(r'^(?:[\w.\-/]+=(?:"(?:[^"\\]|\\.)*"|\S*)\s*){2,}$')
_LOGFMT_PAIR = re.compile(r'([\w.\-/]+)=("(?:[^"\\]|\\.)*"|\S*)')
_MESSAGE_KEYS = ("msg", "message", "error", "err", "reason", "event")
_EXCEPTION = re.compile(
    r"(?:^|\n)\s*((?:[\w.]+\.)?[A-Z]\w*(?:Error|Exception|Warning|Timeout|Failed)\b[^\n]*)"
)


def structured_message(line: str) -> str | None:
    """The part of a line that says what happened, for logfmt and JSON lines
    and Python tracebacks, so field order, extra fields and file paths do not
    make new incidents out of one condition. None for anything else."""
    stripped = line.strip()
    prefix = ""
    # a timestamp / level prefix before a JSON object, e.g. "2026-... {"
    brace = stripped.find("{")
    if brace >= 0 and stripped.endswith("}"):
        try:
            data = json.loads(stripped[brace:])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            level = str(data.get("level") or data.get("severity") or "")
            parts = [str(data[k]) for k in _MESSAGE_KEYS if data.get(k)]
            if parts:
                return f"{level} {' | '.join(parts)}".strip()
    if _LOGFMT_LINE.match(stripped):
        fields = {k: v.strip('"') for k, v in _LOGFMT_PAIR.findall(stripped)}
        parts = [fields[k] for k in _MESSAGE_KEYS if fields.get(k)]
        if parts:
            prefix = fields.get("level", "")
            return f"{prefix} {' | '.join(parts)}".strip()
    if "Traceback" in stripped:
        found = _EXCEPTION.findall(stripped.replace("\\n", "\n"))
        if found:
            return "Traceback: " + str(found[-1])
    return None


def normalize(line: str, pod: str) -> str:
    structured = structured_message(line)
    if structured is not None:
        line = structured
    line = line.replace(pod, "<pod>")
    for pattern, replacement in _POD_NAMES:
        line = pattern.sub(replacement, line)
    for pattern, replacement in _NORMALIZE:
        line = pattern.sub(replacement, line)
    return redact(line.strip())[:MESSAGE_CHARS]


def fingerprint(container: str, workload: str, message: str) -> str:
    digest = hashlib.sha1(f"{container}\n{workload}\n{message}".encode()).hexdigest()
    return digest[:12]


def cluster(lines: list[dict[str, str]]) -> list[Incident]:
    """Group lines by (container, workload, normalized message), most frequent
    first. Samples are redacted before they leave this function."""
    groups: dict[IncidentKey, dict[str, Any]] = {}
    for entry in lines:
        workload = workload_of(entry["pod"])
        message = normalize(entry["line"], entry["pod"])
        if not message:
            continue
        key = IncidentKey(
            fingerprint(entry["container"], workload, message),
            entry["container"],
            workload,
            message,
        )
        group = groups.setdefault(
            key,
            {
                "samples": [],
                "pods": set(),
                "count": 0,
                "first": entry["ts"],
                "last": entry["ts"],
                "first_pod": entry["pod"],
                "first_ts": entry["ts"],
            },
        )
        group["count"] += 1
        group["pods"].add(entry["pod"])
        group["first"] = min(group["first"], entry["ts"])
        group["last"] = max(group["last"], entry["ts"])
        if len(group["samples"]) < MAX_SAMPLES:
            group["samples"].append(
                f"{redact(entry['pod'])}/{entry['container']}: {redact(entry['line'][:MAX_LINE])}"
            )
    incidents = [
        Incident(
            key,
            Evidence(
                g["samples"],
                g["count"],
                len(g["pods"]),
                g["first"],
                g["last"],
                g["first_pod"],
                g["first_ts"],
            ),
        )
        for key, g in groups.items()
    ]
    # Infrastructure first, user workloads after, most frequent first within each.
    incidents.sort(
        key=lambda incident: (
            incident.key.workload in USER_WORKLOADS,
            -incident.evidence.count,
            incident.key.fingerprint,
        )
    )
    return incidents


# ── Root-cause groups ──────────────────────────────────────────────────────────


@dataclass
class Group:
    label: str
    members: list[str]  # fingerprints, the representative first


def grouping_prompt(incidents: list[Incident]) -> str:
    lines = [
        "Group these log incidents from one Kubernetes namespace by root cause: "
        "incidents that would be fixed by the same change, or explained by the same "
        "event, belong together. Different messages from one component about one "
        "condition (a service missing, then its endpoints missing, then the route "
        "failing) are one group. Unrelated incidents stay alone.",
        "",
        "Answer with JSON only: "
        '{"groups": [{"label": "<short cause>", "members": ["<fingerprint>", ...]}, ...]}. '
        "Every fingerprint exactly once. Put the most representative member first.",
        "",
    ]
    for i in incidents:
        lines.append(
            f"- {i.key.fingerprint}: {i.key.workload}/{i.key.container} x{i.evidence.count}: {i.key.message[:200]}"
        )
    return "\n".join(lines)


def parse_groups(text: str, incidents: list[Incident]) -> list[Group]:
    """Groups from the model's reply, made safe: unknown fingerprints are
    dropped, missing ones become groups of their own, an unparsable reply
    means every incident is its own group."""
    known = {i.key.fingerprint: i for i in incidents}
    groups: list[Group] = []
    seen: set[str] = set()
    data = extract_json(text) if "fixable" in text else None
    if data is None:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                value, _ = decoder.raw_decode(text, match.start())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and isinstance(value.get("groups"), list):
                data = value
                break
    for raw in (data or {}).get("groups") or []:
        if not isinstance(raw, dict):
            continue
        members = [
            m
            for m in raw.get("members") or []
            if isinstance(m, str) and m in known and m not in seen
        ]
        if not members:
            continue
        members.sort(key=lambda fp: -known[fp].evidence.count)
        seen.update(members)
        groups.append(
            Group(
                str(raw.get("label") or known[members[0]].key.message[:80])[:120],
                members,
            )
        )
    for fp, incident in known.items():
        if fp not in seen:
            groups.append(Group(incident.key.message[:80], [fp]))
    groups.sort(key=lambda g: -sum(known[fp].evidence.count for fp in g.members))
    return groups


def representative(group: Group, incidents: list[Incident]) -> Incident:
    """The group's first member, carrying the whole group's counts."""
    by_fp = {i.key.fingerprint: i for i in incidents}
    head = by_fp[group.members[0]]
    members = [by_fp[fp] for fp in group.members]
    return Incident(
        head.key,
        Evidence(
            samples=head.evidence.samples,
            count=sum(m.evidence.count for m in members),
            pods=sum(m.evidence.pods for m in members),
            first_seen=min(m.evidence.first_seen for m in members),
            last_seen=max(m.evidence.last_seen for m in members),
            first_pod=head.evidence.first_pod,
            first_ts=head.evidence.first_ts,
        ),
    )


# ── Analysis budget ────────────────────────────────────────────────────────────

CACHE_HIT = "CACHE_HIT"
Spawn = Callable[[Incident], Awaitable[tuple[Verdict, str]]]


async def analyze_within_budget(
    incidents: list[Incident],
    budget: int,
    spawn: Spawn,
    log: Callable[[str], None] = print,
) -> list[tuple[Incident, Verdict | BaseException]]:
    """Analyze incidents in order until `budget` of them were fresh analyses.

    A cache hit costs nothing and does not count, so a list that starts with
    known errors still reaches the new ones. At most `budget` are in flight,
    since an in-flight analysis is assumed fresh until it reports otherwise;
    a hit frees its slot for the next incident. A failure counts as fresh: it
    used a pod. Returns (incident, verdict or exception) for every incident
    attempted, in list order."""
    pending = list(incidents)
    in_flight: dict[asyncio.Task[tuple[Verdict, str]], Incident] = {}
    results: dict[int, Verdict | BaseException] = {}
    order: list[Incident] = []
    fresh = 0
    while pending or in_flight:
        while pending and fresh + len(in_flight) < budget:
            incident = pending.pop(0)
            order.append(incident)
            in_flight[asyncio.ensure_future(spawn(incident))] = incident
        if not in_flight:
            break
        done, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            incident = in_flight.pop(task)
            try:
                verdict, cache = task.result()
            except BaseException as exc:  # noqa: BLE001 - reported per incident
                results[id(incident)] = exc
                fresh += 1
                continue
            results[id(incident)] = verdict
            if cache != CACHE_HIT:
                fresh += 1
            else:
                log(f"{incident.key.fingerprint}: known, from cache")
    return [(incident, results[id(incident)]) for incident in order]


# ── Report ─────────────────────────────────────────────────────────────────────

STATUS_ORDER = ("fixable", "not fixable", "failed", "pending", "not analyzed")


@dataclass
class Row:
    fingerprint: str
    workload: str
    container: str
    count: int
    status: str  # one of STATUS_ORDER
    confidence: float = 0.0
    title: str = ""
    component: str = ""
    reason: str = ""
    pull_request: str = ""
    cache: str = ""
    group: str = ""
    members: int = 1


def report_html(
    tick: str, window: str, rows: list[Row], model: str = "", note: str = ""
) -> str:
    """The triage run's report tab: the verdict count first, then one line per
    incident, fixable ones on top. Plain HTML, inserted into the console's div."""
    analyzed = [r for r in rows if r.status in ("fixable", "not fixable")]
    fixable = [r for r in analyzed if r.status == "fixable"]
    failed = sum(r.status == "failed" for r in rows)
    hits = sum(r.cache == CACHE_HIT for r in rows)
    ordered = sorted(
        rows, key=lambda r: (STATUS_ORDER.index(r.status), -r.count, r.fingerprint)
    )
    e = html.escape
    parts = [
        f"<h2>{len(fixable)} of {len(analyzed)} analyzed incidents fixable in this repository</h2>",
        f"<p>tick <code>{e(tick)}</code>, window {e(window)} UTC: {len(rows)} incidents, "
        f"{len(analyzed)} analyzed ({hits} from cache), {failed} failed, "
        f"{sum(bool(r.pull_request) for r in rows)} pull request(s)"
        + (f", model <code>{e(model)}</code>" if model else "")
        + "</p>",
        *([f"<p><b>{e(note)}</b></p>"] if note else []),
        "<table><thead><tr><th>verdict</th><th>conf.</th><th>incident</th><th>x</th>"
        "<th>title</th><th>component</th><th>PR</th><th>reason</th></tr></thead><tbody>",
    ]
    marks = {
        "fixable": "&#10004; fixable",
        "not fixable": "&#10008; not fixable",
        "failed": "&#9888; failed",
        "pending": "&#8230; pending",
        "not analyzed": "&ndash; not analyzed",
    }
    for r in ordered:
        pr = (
            f'<a href="{e(r.pull_request)}">{e(r.pull_request.rsplit("/", 1)[-1])}</a>'
            if r.pull_request
            else ""
        )
        cache = " (cache)" if r.cache == CACHE_HIT else ""
        parts.append(
            f"<tr><td>{marks[r.status]}{cache}</td>"
            f"<td>{r.confidence:.2f}</td>"
            f"<td><code>{e(r.fingerprint)}</code> {e(r.workload)}/{e(r.container)}"
            + (
                f"<br><small>{e(r.group)} ({r.members} incidents)</small>"
                if r.members > 1
                else ""
            )
            + "</td>"
            f"<td>{r.count}</td><td>{e(r.title)}</td><td>{e(r.component)}</td><td>{pr}</td>"
            f"<td>{e(r.reason[:300])}</td></tr>"
        )
    parts.append("</tbody></table>")
    return "\n".join(parts)


# ── Context around a sample ────────────────────────────────────────────────────

CONTEXT_SECONDS = 5
CONTEXT_LINES = 80


def context_url(base: str, namespace: str, pod: str, container: str, ts: str) -> str:
    """Every line of that container within CONTEXT_SECONDS of the sample: a
    Python traceback arrives as one Loki line per source line, and only the
    header matches the error pattern."""
    moment = datetime.fromisoformat(ts)
    start = moment - timedelta(seconds=CONTEXT_SECONDS)
    end = moment + timedelta(seconds=CONTEXT_SECONDS)
    params = {
        "query": '{namespace="%s", pod="%s", container="%s"}'
        % (namespace, pod, container),
        "start": str(int(start.timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "limit": str(CONTEXT_LINES),
        "direction": "forward",
    }
    return f"{base}/loki/api/v1/query_range?{urllib.parse.urlencode(params)}"


def query_context(
    base: str, namespace: str, pod: str, container: str, ts: str
) -> list[str]:
    """The surrounding lines, redacted, oldest first; empty when Loki fails."""
    try:
        with urllib.request.urlopen(
            context_url(base, namespace, pod, container, ts), timeout=60
        ) as resp:
            entries = parse_loki(json.load(resp))
    except (OSError, ValueError):
        return []
    return [redact(e["line"][:MAX_LINE]) for e in entries]


# ── Changes that are not fixes ─────────────────────────────────────────────────

_LOG_LEVEL = re.compile(
    r"\b(error|exception|critical|fatal|warning|warn|info|debug)\b", re.I
)


def silences(diff: str) -> bool:
    """True when every changed line of a unified diff differs from its
    counterpart only by a log level or wording: the incident is made to go
    away, not fixed."""
    removed = [
        entry[1:]
        for entry in diff.splitlines()
        if entry.startswith("-") and not entry.startswith("---")
    ]
    added = [
        entry[1:]
        for entry in diff.splitlines()
        if entry.startswith("+") and not entry.startswith("+++")
    ]
    if not removed or not added or len(removed) != len(added):
        return False

    def norm(line: str) -> str:
        return _LOG_LEVEL.sub("LEVEL", line).strip()

    return all(
        norm(r) == norm(a) and r.strip() != a.strip() for r, a in zip(removed, added)
    )


def is_rate_limit(error: str) -> bool:
    text = error.lower()
    return (
        "rate limit" in text or '"statuscode": 429' in text or "statuscode=429" in text
    )


# ── opencode's own log ─────────────────────────────────────────────────────────

_LOGFMT = re.compile(r'(\w[\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')
_PROVIDER_ERROR = ("stream error", "AI_APICallError", "rate limit", "APICallError")


def opencode_log_line(raw: str) -> tuple[str, str | None] | None:
    """(summary, provider error message or None) for an ERROR/WARN line of
    ~/.local/share/opencode/log/opencode.log, else None. opencode 1.18 writes
    a provider failure (a rate limit, say) here and nowhere else: not on
    stdout as an event, and the process neither retries nor exits."""
    fields = {k: v.strip('"') for k, v in _LOGFMT.findall(raw)}
    level = fields.get("level", "")
    if level not in ("ERROR", "WARN"):
        return None
    keep = {
        k: v
        for k, v in fields.items()
        if k not in ("timestamp", "run", "session.id", "level")
    }
    summary = f"{level.lower()}: " + " ".join(f"{k}={v}" for k, v in keep.items())
    error = fields.get("error.error") or fields.get("error") or ""
    if any(marker.lower() in raw.lower() for marker in _PROVIDER_ERROR):
        return summary[:300], (error or fields.get("message") or "provider error")[:200]
    return summary[:300], None


# ── Agent output ───────────────────────────────────────────────────────────────


@dataclass
class Reply:
    text: str
    finished: bool
    error: str = ""


def _brief(value: Any, limit: int = 100) -> str:
    if isinstance(value, dict):
        text = " ".join(f"{k}={_brief(v, 60)}" for k, v in list(value.items())[:3])
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_event(event: dict[str, Any]) -> str | None:
    """One log line for an opencode event worth narrating, else None."""
    kind = event.get("type")
    part = event.get("part") or {}
    if kind == "text":
        text = str(part.get("text", "")).strip()
        return f"says: {_brief(text, 200)}" if text else None
    if kind in ("tool", "tool_use", "tool_call", "tool-invocation"):
        state = part.get("state") or {}
        status = str(state.get("status") or "")
        if status and status not in ("completed", "error"):
            return None
        tool = part.get("tool") or part.get("name") or "tool"
        detail = state.get("title") or _brief(
            state.get("input") or part.get("input") or ""
        )
        return f"{tool} {status}: {detail}".replace("  ", " ").strip()
    if kind == "step_finish":
        tokens = part.get("tokens") or {}
        return (
            f"step {part.get('reason', '?')} "
            f"({tokens.get('input', 0)} in / {tokens.get('output', 0)} out tokens)"
        )
    if kind == "error":
        return f"error: {_brief(event.get('error') or part, 300)}"
    return None


def collect_reply(
    events: Iterable[str], observe: Callable[[dict[str, Any]], None] | None = None
) -> Reply:
    """Fold `opencode run --format json` events into the assistant's text.

    Stops consuming at the step that ends with reason "stop": the model's
    final turn. opencode 1.18 keeps the process alive after that, so the
    caller must not wait for exit before reading the answer. `observe` sees
    every parsed event as it arrives."""
    parts: list[str] = []
    for raw in events:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if observe is not None:
            observe(event)
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "text":
            parts.append(str(part.get("text", "")))
        elif kind == "error":
            detail = event.get("error") or part
            return Reply("\n".join(parts), False, json.dumps(detail)[:2000])
        elif kind == "step_finish" and part.get("reason") == "stop":
            return Reply("\n".join(parts), True)
    return Reply(
        "\n".join(parts), False, "the event stream ended before the model's final turn"
    )


def extract_json(text: str) -> dict[str, Any] | None:
    """The last JSON object in the agent's reply that carries a `fixable` key."""
    decoder = json.JSONDecoder()
    found = None
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "fixable" in value:
            found = value
    return found


def parse_verdict(text: str) -> Verdict:
    data = extract_json(text)
    if data is None:
        return Verdict(fixable=False, reason="the agent gave no verdict")
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return Verdict(
        fixable=bool(data.get("fixable")),
        confidence=confidence,
        component=str(data.get("component", "")),
        title=str(data.get("title", ""))[:120],
        reason=str(data.get("reason", "")),
        plan=str(data.get("plan", "")),
    )


# ── GitHub ─────────────────────────────────────────────────────────────────────


def github(
    method: str, path: str, token: str, body: dict[str, Any] | None = None
) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "purdue-af-self-repair",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as resp:
        return json.load(resp)


def open_pull_request(repo: str, branch: str, token: str) -> str:
    """URL of the open PR from `branch`, or "" when there is none."""
    owner = repo.split("/")[0]
    query = urllib.parse.urlencode({"head": f"{owner}:{branch}", "state": "open"})
    pulls = github("GET", f"/repos/{repo}/pulls?{query}", token)
    return pulls[0]["html_url"] if pulls else ""


LABEL = "self-repair"
TITLE_PREFIX = "[self-repair] "


def ensure_label(repo: str, token: str) -> None:
    try:
        github("GET", f"/repos/{repo}/labels/{LABEL}", token)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        github(
            "POST",
            f"/repos/{repo}/labels",
            token,
            {
                "name": LABEL,
                "color": "B60205",
                "description": "Opened by the self-repair workflow; review with care",
            },
        )


def create_pull_request(
    repo: str, token: str, branch: str, base: str, title: str, body: str
) -> str:
    """A draft PR, titled and labelled so it is never mistaken for a human's."""
    pull = github(
        "POST",
        f"/repos/{repo}/pulls",
        token,
        {
            "title": TITLE_PREFIX + title,
            "head": branch,
            "base": base,
            "body": body,
            "draft": True,
        },
    )
    ensure_label(repo, token)
    github(
        "POST",
        f"/repos/{repo}/issues/{pull['number']}/labels",
        token,
        {"labels": [LABEL]},
    )
    return str(pull["html_url"])


def pull_request_body(
    key: IncidentKey,
    evidence: Evidence,
    verdict: Verdict,
    agent_summary: str,
    run_name: str,
    model: str,
) -> str:
    samples = "\n".join(evidence.samples)
    return f"""{verdict.reason}

## Plan

{verdict.plan}

## What the agent changed

{agent_summary.strip() or "(no summary)"}

## Evidence

| | |
| --- | --- |
| Workload | `{key.workload}` / `{key.container}` |
| Occurrences | {evidence.count} in {evidence.pods} pod(s), {evidence.first_seen} to {evidence.last_seen} |
| Fingerprint | `{key.fingerprint}` |

```
{samples}
```

---
Opened by the [self-repair](workflows/self-repair) workflow, Flyte run `{run_name}`, model `{model}`. Draft on purpose: a human reviews before this merges.
"""


# ── Model choice ───────────────────────────────────────────────────────────────


@dataclass
class Probe:
    model: str
    available: bool
    seconds: float
    detail: str = ""


def probe_body(model_id: str) -> dict[str, Any]:
    """The smallest completion that proves a model is answering."""
    return {
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "stream": False,
        "max_tokens": 8,
    }


def pick_model(
    models: Iterable[str], probe: Callable[[str], tuple[bool, float, str]]
) -> tuple[str | None, list[Probe]]:
    """The first model that answers its probe, in the given order, and what
    every probe up to it said. Nothing after it is probed: the tick has its
    model. None when no model answers."""
    probes: list[Probe] = []
    for model in models:
        available, seconds, detail = probe(model)
        probes.append(Probe(model, available, seconds, detail))
        if available:
            return model, probes
    return None, probes


# ── Metrics ────────────────────────────────────────────────────────────────────
# One push per tick to the AF Prometheus pushgateway (job `self-repair`). The
# `_total` series are counters carried across ticks: the previous values are
# read back from the gateway and re-pushed incremented, so `increase()` works
# in Grafana. The `last_tick_*` and `model_*` gauges describe the newest tick.

OUTCOMES = ("ok", "no_model", "failed")
RESULTS = ("fresh", "cache_hit", "failed")

METRICS: dict[str, tuple[str, str]] = {
    "self_repair_ticks_total": ("counter", "Ticks by outcome"),
    "self_repair_analyses_total": ("counter", "Analyses by result"),
    "self_repair_fixable_total": (
        "counter",
        "Incidents judged fixable in this repository",
    ),
    "self_repair_pull_requests_total": ("counter", "Draft pull requests opened"),
    "self_repair_fix_failures_total": (
        "counter",
        "Fix attempts that ended without a pull request",
    ),
    "self_repair_model_probes_total": ("counter", "Model probes by model and answer"),
    "self_repair_last_tick_timestamp_seconds": (
        "gauge",
        "When the newest tick started",
    ),
    "self_repair_last_tick_duration_seconds": (
        "gauge",
        "How long the newest tick took",
    ),
    "self_repair_last_tick_outcome": ("gauge", "1 for the newest tick's outcome"),
    "self_repair_last_tick_error_lines": (
        "gauge",
        "Error lines read by the newest tick",
    ),
    "self_repair_last_tick_incidents": (
        "gauge",
        "Distinct incidents in the newest tick",
    ),
    "self_repair_last_tick_groups": ("gauge", "Root-cause groups in the newest tick"),
    "self_repair_last_tick_analyses": (
        "gauge",
        "Analyses in the newest tick by result",
    ),
    "self_repair_last_tick_fixable": ("gauge", "Fixable incidents in the newest tick"),
    "self_repair_last_tick_pull_requests": (
        "gauge",
        "Pull requests opened by the newest tick",
    ),
    "self_repair_model_available": ("gauge", "1 when the model answered its probe"),
    "self_repair_model_probe_seconds": ("gauge", "Probe latency in the newest tick"),
    "self_repair_model_selected": ("gauge", "1 for the model the newest tick ran on"),
}


@dataclass
class TickMetrics:
    started: float  # unix seconds
    outcome: str
    model: str
    models: list[str]
    probes: list[Probe] = field(default_factory=list)
    duration: float = 0.0
    error_lines: int = 0
    incidents: int = 0
    groups: int = 0
    analyses: dict[str, int] = field(default_factory=dict)  # result -> count
    fixable: int = 0
    pull_requests: int = 0
    fix_failures: int = 0


Labels = tuple[tuple[str, str], ...]
Series = tuple[str, Labels]

_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
_GATEWAY_LABELS = ("job", "instance")


def parse_counters(text: str) -> dict[Series, float]:
    """Our counters in a pushgateway /metrics page; the labels the gateway
    adds (job, instance) are dropped so the series match what we push."""
    counters: dict[Series, float] = {}
    for line in text.splitlines():
        match = _SAMPLE.match(line)
        if not match:
            continue
        name, raw_labels, value = match.groups()
        if name not in METRICS or METRICS[name][0] != "counter":
            continue
        labels = tuple(
            (k, v.encode().decode("unicode_escape"))
            for k, v in _LABEL.findall(raw_labels or "")
            if k not in _GATEWAY_LABELS
        )
        try:
            counters[(name, tuple(sorted(labels)))] = float(value)
        except ValueError:
            continue
    return counters


def _labels(labels: Labels) -> str:
    if not labels:
        return ""
    escaped = ",".join(
        f'{k}="{v.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for k, v in labels
    )
    return "{" + escaped + "}"


def exposition(metrics: TickMetrics, previous: dict[Series, float]) -> str:
    """The text to PUT to the gateway: every counter it already had, plus this
    tick's increments, plus this tick's gauges. Only names in METRICS."""
    counters = dict(previous)

    def add(name: str, labels: Labels, amount: float) -> None:
        key = (name, tuple(sorted(labels)))
        counters[key] = counters.get(key, 0.0) + amount

    add("self_repair_ticks_total", (("outcome", metrics.outcome),), 1)
    for result, count in metrics.analyses.items():
        add("self_repair_analyses_total", (("result", result),), count)
    add("self_repair_fixable_total", (), metrics.fixable)
    add("self_repair_pull_requests_total", (), metrics.pull_requests)
    add("self_repair_fix_failures_total", (), metrics.fix_failures)
    for probe in metrics.probes:
        add(
            "self_repair_model_probes_total",
            (("model", probe.model), ("available", str(probe.available).lower())),
            1,
        )

    gauges: list[tuple[str, Labels, float]] = [
        ("self_repair_last_tick_timestamp_seconds", (), metrics.started),
        ("self_repair_last_tick_duration_seconds", (), metrics.duration),
        ("self_repair_last_tick_error_lines", (), metrics.error_lines),
        ("self_repair_last_tick_incidents", (), metrics.incidents),
        ("self_repair_last_tick_groups", (), metrics.groups),
        ("self_repair_last_tick_fixable", (), metrics.fixable),
        ("self_repair_last_tick_pull_requests", (), metrics.pull_requests),
    ]
    for outcome in OUTCOMES:
        gauges.append(
            (
                "self_repair_last_tick_outcome",
                (("outcome", outcome),),
                float(outcome == metrics.outcome),
            )
        )
    for result in RESULTS:
        gauges.append(
            (
                "self_repair_last_tick_analyses",
                (("result", result),),
                metrics.analyses.get(result, 0),
            )
        )
    for probe in metrics.probes:
        gauges.append(
            (
                "self_repair_model_available",
                (("model", probe.model),),
                float(probe.available),
            )
        )
        gauges.append(
            (
                "self_repair_model_probe_seconds",
                (("model", probe.model),),
                probe.seconds,
            )
        )
    for model in metrics.models:
        gauges.append(
            (
                "self_repair_model_selected",
                (("model", model),),
                float(model == metrics.model),
            )
        )

    samples: dict[str, list[tuple[Labels, float]]] = {name: [] for name in METRICS}
    for (name, labels), value in sorted(counters.items()):
        samples[name].append((labels, value))
    for name, labels, value in gauges:
        samples[name].append((labels, value))
    lines: list[str] = []
    for name, (kind, help_text) in METRICS.items():
        if not samples[name]:
            continue
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples[name]:
            number = str(int(value)) if value == int(value) else repr(float(value))
            lines.append(f"{name}{_labels(labels)} {number}")
    return "\n".join(lines) + "\n"
