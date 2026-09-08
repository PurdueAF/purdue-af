"""Identify the agent behind an MCP request, and where the request came from.

`client` comes from the `clientInfo` of the MCP `initialize` request, which is
the only message that carries it, so it is remembered against the
`Mcp-Session-Id` and reused for the rest of the session; requests with no known
session fall back to the User-Agent. `origin` says whether the request arrived
through the JupyterHub proxy or straight to the ClusterIP Service.

Both are caller-supplied strings, so both are clamped to an allowlist before
they become Prometheus labels. The raw name still reaches the audit line.
"""

import json
import time
from typing import Any, Mapping, NamedTuple, Optional

_CACHE_TTL = 24 * 3600.0
_CACHE_MAX = 4096

UNKNOWN = "unknown"
OTHER = "other"

_MAX_RAW = 120


class ClientInfo(NamedTuple):
    name: str  # allowlisted slug, safe as a label value
    raw: str  # verbatim, for the audit line only
    version: str


_UNKNOWN_CLIENT = ClientInfo(name=UNKNOWN, raw="", version="")

# Substring → slug, first match wins. Add an entry only once the raw name is
# seen arriving, so the label set stays small enough for a dashboard legend.
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

_sessions: dict[str, tuple[float, ClientInfo]] = {}

# Characters that would let a caller forge fields in the logfmt audit line.
_LOGFMT_UNSAFE = str.maketrans({"\n": " ", "\r": " ", '"': "", "\\": ""})


def classify(name: str) -> str:
    lowered = name.lower()
    for pattern, slug in _CLIENT_PATTERNS:
        if pattern in lowered:
            return slug
    return OTHER


def _clean(value: Any, limit: int = _MAX_RAW) -> str:
    """Coerce an untrusted JSON value to a short, logfmt-safe string."""
    if not isinstance(value, str):
        return ""
    return value.translate(_LOGFMT_UNSAFE).strip()[:limit]


def client_from_initialize(body: bytes) -> Optional[ClientInfo]:
    """`clientInfo` out of a JSON-RPC `initialize` body; None for anything else."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None

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
    product, _, _ = agent.partition(" ")
    name, _, version = product.partition("/")
    return ClientInfo(
        name=classify(agent), raw=name or agent, version=_clean(version, 40)
    )


def _evict(now: float) -> None:
    if len(_sessions) < _CACHE_MAX:
        return
    for key in [k for k, (expiry, _) in _sessions.items() if expiry <= now]:
        del _sessions[key]
    while len(_sessions) >= _CACHE_MAX:
        del _sessions[min(_sessions, key=lambda k: _sessions[k][0])]


def remember(session_id: str, client: ClientInfo) -> None:
    if not session_id:
        return
    now = time.monotonic()
    _evict(now)
    _sessions[session_id] = (now + _CACHE_TTL, client)


def lookup(session_id: str) -> Optional[ClientInfo]:
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
    _sessions.pop(session_id, None)


def reset_sessions() -> None:
    """Tests only."""
    _sessions.clear()


def session_id_of(headers: Mapping[bytes, bytes]) -> str:
    return _clean(headers.get(b"mcp-session-id", b"").decode(errors="replace"), 200)


_IN_CLUSTER_SUFFIXES = (".svc.cluster.local", ".svc", ".cluster.local")
_IN_CLUSTER_HOSTS = frozenset({"agentic-interface", "localhost", "127.0.0.1", "::1"})


def origin_of(headers: Mapping[bytes, bytes]) -> str:
    """`in_session` or `external`, by ingress path rather than by who is asking.

    An in-session agent pointed at the public URL reads as `external`, because
    that is the route the request took.
    """
    if b"x-forwarded-for" in headers:
        return "external"
    host = headers.get(b"host", b"").decode(errors="replace").strip().lower()
    if not host:
        return UNKNOWN
    if not host.startswith("[") and host.count(":") == 1:
        host = host.split(":", 1)[0]
    if host in _IN_CLUSTER_HOSTS or host.endswith(_IN_CLUSTER_SUFFIXES):
        return "in_session"
    return "external"
