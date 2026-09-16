"""Everything in the self-repair workflow that is not a Flyte task: the Loki
query, error fingerprints, username redaction, the agent's verdict and the
GitHub calls. No flyte import, so the tests run without the SDK."""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

ERROR_PATTERN = r"(?i)\b(error|exception|traceback|fatal|panic)\b"
MAX_SAMPLES = 8
MAX_LINE = 400
MESSAGE_CHARS = 240

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


# ── Loki ───────────────────────────────────────────────────────────────────────


def loki_url(
    base: str, namespace: str, start: datetime, end: datetime, limit: int
) -> str:
    query = '{namespace="%s"} |~ "%s"' % (
        namespace,
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
    base: str, namespace: str, start: datetime, end: datetime, limit: int = 5000
) -> list[dict[str, str]]:
    """Every matching line in the window as {pod, container, ts, line}."""
    with urllib.request.urlopen(
        loki_url(base, namespace, start, end, limit), timeout=60
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


def normalize(line: str, pod: str) -> str:
    line = line.replace(pod, "<pod>")
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
            Evidence(g["samples"], g["count"], len(g["pods"]), g["first"], g["last"]),
        )
        for key, g in groups.items()
    ]
    incidents.sort(
        key=lambda incident: (-incident.evidence.count, incident.key.fingerprint)
    )
    return incidents


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


def create_pull_request(
    repo: str, token: str, branch: str, base: str, title: str, body: str
) -> str:
    pull = github(
        "POST",
        f"/repos/{repo}/pulls",
        token,
        {"title": title, "head": branch, "base": base, "body": body, "draft": True},
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
