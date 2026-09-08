"""Identify the agent behind an MCP request, and where the request came from.

Two dimensions the tool metrics could not previously distinguish:

``client``
    Which harness is driving the session — Claude Code, Codex, opencode, a
    jupyter-ai persona, Cursor, a hand-written script. MCP carries this in the
    ``clientInfo`` object of the ``initialize`` request and **nowhere else**,
    so it is captured once per session and remembered against the
    ``Mcp-Session-Id`` the server hands back. Requests that arrive without a
    known session (stateless mode, or a client that skipped the handshake)
    fall back to the User-Agent header.

``origin``
    Whether the request arrived through the JupyterHub proxy — a laptop, or
    anything else outside the cluster — or straight to the ClusterIP Service,
    which only an AF session can reach.

Both are clamped to a small allowlist before they become Prometheus labels:
``clientInfo.name`` and User-Agent are attacker-controlled strings, and an
unbounded label value is an unbounded number of time series. The raw name
still reaches the ``tool_call`` audit line in Loki, so a harness nobody
anticipated shows up there and can then be added to ``_CLIENT_PATTERNS``.
"""

import json
import time
from typing import Any, Mapping, NamedTuple, Optional

# A session id is remembered for as long as an agent might plausibly hold one
# open. Sessions are dropped oldest-first past _CACHE_MAX; losing an entry
# costs a "unknown" client label on later calls, nothing more.
_CACHE_TTL = 24 * 3600.0
_CACHE_MAX = 4096

UNKNOWN = "unknown"


class ClientInfo(NamedTuple):
    """What the client called itself, and what we label it as."""

    # Allowlisted slug, safe as a Prometheus label value.
    name: str
    # Verbatim clientInfo.name (or User-Agent product), for the audit log only.
    raw: str
    version: str


_UNKNOWN_CLIENT = ClientInfo(name=UNKNOWN, raw="", version="")

# Substring → slug, first match wins. Matched case-insensitively against
# clientInfo.name and, failing that, the User-Agent. Deliberately short: a
# new entry should be added only once Loki shows the raw name actually
# arriving, so the label set stays something a dashboard legend can hold.
_CLIENT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("claude-code", "claude-code"),
    ("claude code", "claude-code"),
    ("claude-agent-acp", "claude-code"),
    ("codex", "codex"),
    ("opencode", "opencode"),
    ("jupyter", "jupyter-ai"),
    ("cursor", "cursor"),
    ("continue", "continue"),
    ("windsurf", "windsurf"),
    ("cline", "cline"),
    ("zed", "zed"),
    ("inspector", "mcp-inspector"),
    ("vscode", "vscode"),
    ("code-server", "vscode"),
    ("curl", "curl"),
    ("httpx", "script"),
    ("python-requests", "script"),
)

# Everything that matched nothing above. Kept distinct from "unknown" (which
# means we never learned a name at all) so the dashboard can tell "a harness
# we have not classified yet" from "no handshake seen".
OTHER = "other"

_MAX_RAW = 120

_sessions: dict[str, tuple[float, ClientInfo]] = {}


def classify(name: str) -> str:
    """Map a self-reported client name onto an allowlisted label value."""
    lowered = name.lower()
    for pattern, slug in _CLIENT_PATTERNS:
        if pattern in lowered:
            return slug
    return OTHER


def _clean(value: Any, limit: int = _MAX_RAW) -> str:
    """Coerce an untrusted JSON value to a short single-line string."""
    if not isinstance(value, str):
        return ""
    # Newlines in a value would forge extra lines in the logfmt audit record.
    return value.strip().replace("\n", " ").replace("\r", " ")[:limit]


def client_from_initialize(body: bytes) -> Optional[ClientInfo]:
    """Extract ``clientInfo`` from a JSON-RPC ``initialize`` request body.

    Returns None for any other message, and for anything that is not the
    JSON the MCP spec promises — a malformed body is the transport's problem
    to report, not this module's.
    """
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None

    # The JSON-RPC batch form is a list; MCP clients rarely use it for
    # initialize, but it costs one line to not be surprised by it.
    messages = payload if isinstance(payload, list) else [payload]
    for message in messages:
        if not isinstance(message, dict) or message.get("method") != "initialize":
            continue
        params = message.get("params")
        info = params.get("clientInfo") if isinstance(params, dict) else None
        if not isinstance(info, dict):
            continue
        raw = _clean(info.get("name"))
        if not raw:
            continue
        return ClientInfo(
            name=classify(raw), raw=raw, version=_clean(info.get("version"), 40)
        )
    return None


def from_user_agent(headers: Mapping[bytes, bytes]) -> ClientInfo:
    """Best-effort identification for a request with no known MCP session."""
    agent = _clean(headers.get(b"user-agent", b"").decode(errors="replace"))
    if not agent:
        return _UNKNOWN_CLIENT
    # "claude-code/2.1.263 (foo)" → product token carries name and version.
    product, _, _ = agent.partition(" ")
    name, _, version = product.partition("/")
    return ClientInfo(
        name=classify(agent), raw=name or agent, version=_clean(version, 40)
    )


def _evict(now: float) -> None:
    """Keep the session cache bounded: expired entries first, then oldest."""
    if len(_sessions) < _CACHE_MAX:
        return
    for key in [k for k, (expiry, _) in _sessions.items() if expiry <= now]:
        del _sessions[key]
    while len(_sessions) >= _CACHE_MAX:
        del _sessions[min(_sessions, key=lambda k: _sessions[k][0])]


def remember(session_id: str, client: ClientInfo) -> None:
    """Associate an MCP session id with the client that opened it."""
    if not session_id:
        return
    now = time.monotonic()
    _evict(now)
    _sessions[session_id] = (now + _CACHE_TTL, client)


def lookup(session_id: str) -> Optional[ClientInfo]:
    """The client that opened this MCP session, if we still remember it."""
    if not session_id:
        return None
    entry = _sessions.get(session_id)
    if entry is None:
        return None
    expiry, client = entry
    if time.monotonic() >= expiry:
        del _sessions[session_id]
        return None
    return client


def forget(session_id: str) -> None:
    """Drop a session (its DELETE arrived, or the handshake failed)."""
    _sessions.pop(session_id, None)


def reset_sessions() -> None:
    """Clear the cache. Tests only."""
    _sessions.clear()


def session_id_of(headers: Mapping[bytes, bytes]) -> str:
    return _clean(headers.get(b"mcp-session-id", b"").decode(errors="replace"), 200)


# Hostnames that only something inside the cluster can be talking to. An AF
# session reaches this service at the in-cluster address config-agents.sh
# registers; a laptop reaches it at the facility's public hostname, through
# the JupyterHub proxy.
_IN_CLUSTER_SUFFIXES = (".svc.cluster.local", ".svc", ".cluster.local")
_IN_CLUSTER_HOSTS = frozenset({"agentic-interface", "localhost", "127.0.0.1", "::1"})


def origin_of(headers: Mapping[bytes, bytes]) -> str:
    """Classify where a request entered from: ``in_session`` or ``external``.

    This measures the ingress path, not the human: a user who points their
    in-session agent at the public URL is reported as ``external``, because
    that is genuinely the route the request took. The Host header is the
    primary signal (the client sets it from the URL it was configured with),
    with an X-Forwarded-For — which only the proxy adds — as a backstop.
    """
    if b"x-forwarded-for" in headers:
        return "external"
    host = headers.get(b"host", b"").decode(errors="replace").strip().lower()
    if not host:
        return UNKNOWN
    # Strip the port, leaving bracketed IPv6 literals alone.
    if not host.startswith("[") and host.count(":") == 1:
        host = host.split(":", 1)[0]
    if host in _IN_CLUSTER_HOSTS or host.endswith(_IN_CLUSTER_SUFFIXES):
        return "in_session"
    return "external"
