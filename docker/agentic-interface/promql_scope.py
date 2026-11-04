"""What a caller's PromQL may see: every series in cms, and of the series that
belong to one person, only the caller's own.

Every selector of a query gets one matcher per label below (promql-scope adds
them). A matcher that also matches the empty string still selects every series
without that label, so a series that names no person is not limited.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from config import NAMESPACE, PROMETHEUS_URL
from errors import UpstreamError, UserError, describe_exception, json_body

SCOPE_BINARY = "promql-scope"
SCOPE_TIMEOUT = 10.0

# Labels that name a namespace: only cms, or none.
NAMESPACE_LABELS = (
    "namespace",
    "exported_namespace",
    "claim_namespace",
    "service_namespace",
)
# Labels whose every value is a person.
IDENTITY_LABELS = ("username", "user")
# Loki's single tenant, in its own `user` label.
SHARED_IDENTITIES = ("fake",)
# Labels that name Kubernetes objects, some of them a person's.
OBJECT_LABELS = (
    "configmap",
    "created_by_name",
    "deployment",
    "endpoint",
    "exported_job",
    "exported_pod",
    "exported_service",
    "job",
    "job_name",
    "name",
    "networkpolicy",
    "owner",
    "owner_name",
    "persistentvolumeclaim",
    "pod",
    "ray_cluster",
    "replicaset",
    "secret",
    "service",
    "service_account",
    "volume",
    "workload",
)
# Name prefixes of a person's objects: sessions, Dask clusters, Ray, home PVCs.
USER_PREFIXES = (
    *(f"purdue-af-{d}" for d in "0123456789"),
    "dask-scheduler-",
    "dask-worker-",
    *(f"dask-{h}" for h in "0123456789abcdef"),
    "ray-job-",
    "ray-train-",
    "claim-",
    "volume-",
)
# A component named with a user prefix.
SHARED_PREFIXES = ("ray-train-gateway",)

SESSION_POD = re.compile(r"purdue-af-\d+")
DASK_SCHEDULER = re.compile(r"dask-scheduler-([0-9a-f]+)")
OWNED_PVC_QUERY = "kube_pod_spec_volumes_persistentvolumeclaims_info"


def _class(chars: set[str]) -> str:
    return "".join("\\" + c if c in "\\]^-[" else c for c in sorted(chars))


def without_prefixes(prefixes: tuple[str, ...]) -> str:
    """A regex for the strings that start with none of ``prefixes``."""
    trie: dict[str, Any] = {}
    for prefix in prefixes:
        node = trie
        for char in prefix:
            node = node.setdefault(char, {})
        node[""] = True

    def walk(node: dict[str, Any]) -> str:
        chars = {c for c in node if c}
        parts = ["", f"[^{_class(chars)}].*"]
        for char in sorted(chars):
            child = node[char]
            if "" not in child:
                parts.append(re.escape(char) + walk(child))
        return "(?:" + "|".join(parts) + ")"

    return walk(trie)


NOT_A_PERSONS = without_prefixes(USER_PREFIXES)


@dataclass
class Owned:
    """A caller and the objects of theirs that series name."""

    username: str
    pods: set[str] = field(default_factory=set)
    dask_clusters: set[str] = field(default_factory=set)
    claims: set[str] = field(default_factory=set)
    volumes: set[str] = field(default_factory=set)

    def object_names(self) -> list[str]:
        """Regexes for the names of the caller's objects."""
        names = [re.escape(n) for n in sorted(self.pods | self.claims | self.volumes)]
        names += [
            f"dask-(?:scheduler-|worker-|credentials-)?{re.escape(c)}(?:-.*)?"
            for c in sorted(self.dask_clusters)
        ]
        return names


def matchers(owned: Owned) -> list[dict[str, str]]:
    """The matchers every selector of the caller's query must also satisfy."""
    out = [
        {"name": n, "type": "=~", "value": f"|{NAMESPACE}"} for n in NAMESPACE_LABELS
    ]
    people = [re.escape(p) for p in (*SHARED_IDENTITIES, owned.username) if p]
    out += [
        {"name": n, "type": "=~", "value": "|".join(["", *people])}
        for n in IDENTITY_LABELS
    ]
    allowed = "|".join(
        [NOT_A_PERSONS]
        + [re.escape(p) + ".*" for p in SHARED_PREFIXES]
        + owned.object_names()
    )
    out += [{"name": n, "type": "=~", "value": allowed} for n in OBJECT_LABELS]
    return out


async def scope(query: str, owned: Owned) -> str:
    """``query`` with every selector limited to what ``owned`` may see."""
    request = json.dumps({"query": query, "matchers": matchers(owned)}).encode()
    try:
        proc = await asyncio.create_subprocess_exec(
            SCOPE_BINARY,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(request), SCOPE_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as exc:
        raise UpstreamError(
            f"Error: could not check the query — {describe_exception(exc)}."
        ) from exc
    try:
        answer = json.loads(out)
    except ValueError as exc:
        raise UpstreamError("Error: could not check the query.") from exc
    if answer.get("error"):
        raise UserError(f"Error: the query does not parse — {answer['error']}.")
    return str(answer["query"])


async def _series(
    client: httpx.AsyncClient, match: list[str], start: float, end: float
) -> list[dict[str, str]]:
    try:
        resp = await client.get(
            f"{PROMETHEUS_URL}/api/v1/series",
            params={"match[]": match, "start": f"{start:.3f}", "end": f"{end:.3f}"},
            timeout=20.0,
        )
    except httpx.RequestError as exc:
        raise UpstreamError(
            f"Error: could not tell which series are yours — Prometheus is "
            f"unreachable ({describe_exception(exc)})."
        ) from exc
    body = json_body(resp)
    data = body.get("data") if isinstance(body, dict) else None
    if resp.status_code != 200 or not isinstance(data, list):
        raise UpstreamError(
            "Error: could not tell which series are yours — Prometheus returned "
            f"HTTP {resp.status_code}."
        )
    return [s for s in data if isinstance(s, dict)]


def _quote(value: str) -> str:
    return json.dumps(value)


async def resolve(
    client: httpx.AsyncClient, username: str, start: float, end: float
) -> Owned:
    """The caller's objects that series name between ``start`` and ``end``:
    their session pod and Dask clusters, as the AF Prometheus labels them, and
    the PVCs that pod mounts."""
    owned = Owned(username)
    who = _quote(username)
    for series in await _series(
        client, [f"{{username={who}}}", f"{{user={who}}}"], start, end
    ):
        pod = series.get("pod", "")
        if SESSION_POD.fullmatch(pod):
            owned.pods.add(pod)
        elif m := DASK_SCHEDULER.fullmatch(pod):
            owned.dask_clusters.add(m.group(1))
    if owned.pods:
        pods = "|".join(re.escape(p) for p in sorted(owned.pods))
        selector = (
            f"{OWNED_PVC_QUERY}{{namespace={_quote(NAMESPACE)},pod=~{_quote(pods)}}}"
        )
        for series in await _series(client, [selector], start, end):
            if claim := series.get("persistentvolumeclaim"):
                owned.claims.add(claim)
            if volume := series.get("volume"):
                owned.volumes.add(volume)
    return owned
