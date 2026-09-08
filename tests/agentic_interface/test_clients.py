"""Tests for caller identification (clients.py) and its use in the middleware.

Two things are worth pinning down here. The first is that neither label can be
driven to arbitrary cardinality by a caller: `client` comes from a string the
client chose for itself, and a Prometheus label with unbounded values is a
Prometheus outage waiting to happen. The second is the stateful path — a
clientInfo arrives once, on `initialize`, and every later tool call has to be
attributed from the Mcp-Session-Id alone.
"""

import json

import clients
import pytest
import server
from context import current_client, current_origin, current_session
from mcp.server.auth.provider import AccessToken
from prometheus_client import REGISTRY

PREFIX = server.SERVICE_PREFIX


@pytest.fixture(autouse=True)
def clean_sessions():
    clients.reset_sessions()
    yield
    clients.reset_sessions()


# ── classification ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("claude-code", "claude-code"),
        ("Claude Code", "claude-code"),
        ("claude-agent-acp", "claude-code"),
        ("codex-mcp-client", "codex"),
        ("opencode", "opencode"),
        ("jupyter-ai", "jupyter-ai"),
        ("Cursor", "cursor"),
        ("mcp-inspector", "mcp-inspector"),
        ("python-httpx/0.27", "script"),
        ("something-nobody-has-shipped", "other"),
        ("", "other"),
    ],
)
def test_classify_maps_onto_the_allowlist(raw, expected):
    assert clients.classify(raw) == expected


def test_classify_never_returns_a_caller_controlled_string():
    """The whole point of the allowlist: no unbounded label cardinality."""
    allowed = {slug for _, slug in clients._CLIENT_PATTERNS} | {clients.OTHER}
    for hostile in ("../../etc/passwd", "a" * 5000, "x\ny", "🙂", "{}"):
        assert clients.classify(hostile) in allowed


# ── clientInfo out of an initialize body ──────────────────────────────────────


def _initialize(name="claude-code", version="2.1.263"):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": name, "version": version},
            },
        }
    ).encode()


def test_client_from_initialize_reads_name_and_version():
    info = clients.client_from_initialize(_initialize())
    assert info is not None
    assert (info.name, info.raw, info.version) == (
        "claude-code",
        "claude-code",
        "2.1.263",
    )


def test_client_from_initialize_keeps_the_raw_name_for_the_audit_line():
    """An unclassified harness must still be identifiable in Loki."""
    info = clients.client_from_initialize(_initialize(name="brand-new-agent"))
    assert info is not None
    assert info.name == "other" and info.raw == "brand-new-agent"


def test_client_from_initialize_strips_newlines_from_the_raw_name():
    """A newline in the raw name would forge extra lines in the logfmt record."""
    info = clients.client_from_initialize(_initialize(name="evil\nclient_raw=spoofed"))
    assert info is not None
    assert "\n" not in info.raw and "\r" not in info.raw


def test_client_from_initialize_bounds_the_raw_name():
    info = clients.client_from_initialize(_initialize(name="z" * 9000))
    assert info is not None
    assert len(info.raw) <= clients._MAX_RAW


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"x"}}',
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{}}}',
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":"nonsense"}',
    ],
)
def test_client_from_initialize_returns_none_for_anything_else(body):
    assert clients.client_from_initialize(body) is None


def test_client_from_initialize_handles_a_jsonrpc_batch():
    batch = json.dumps(
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            json.loads(_initialize(name="opencode")),
        ]
    ).encode()
    info = clients.client_from_initialize(batch)
    assert info is not None and info.name == "opencode"


# ── User-Agent fallback ───────────────────────────────────────────────────────


def test_from_user_agent_splits_product_and_version():
    info = clients.from_user_agent({b"user-agent": b"claude-code/2.1.263 (extra)"})
    assert (info.name, info.raw, info.version) == (
        "claude-code",
        "claude-code",
        "2.1.263",
    )


def test_from_user_agent_without_a_header_is_unknown():
    info = clients.from_user_agent({})
    assert info.name == clients.UNKNOWN


# ── origin ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "headers, expected",
    [
        ({b"host": b"agentic-interface.cms.svc.cluster.local:8888"}, "in_session"),
        ({b"host": b"agentic-interface"}, "in_session"),
        ({b"host": b"localhost:8888"}, "in_session"),
        ({b"host": b"cms.geddes.rcac.purdue.edu"}, "external"),
        # The proxy hop is decisive even when the Host survived it.
        (
            {
                b"host": b"agentic-interface.cms.svc.cluster.local",
                b"x-forwarded-for": b"10.0.0.1",
            },
            "external",
        ),
        ({}, "unknown"),
    ],
)
def test_origin_of(headers, expected):
    assert clients.origin_of(headers) == expected


# ── the session cache ─────────────────────────────────────────────────────────


def test_remember_and_lookup_round_trip():
    info = clients.ClientInfo(name="codex", raw="codex", version="0.153.4")
    clients.remember("sess-1", info)
    assert clients.lookup("sess-1") == info
    clients.forget("sess-1")
    assert clients.lookup("sess-1") is None


def test_lookup_without_a_session_id_is_none():
    assert clients.lookup("") is None


def test_expired_sessions_are_dropped(monkeypatch):
    info = clients.ClientInfo(name="codex", raw="codex", version="0")
    clients.remember("sess-2", info)
    now = clients.time.monotonic()
    monkeypatch.setattr(clients.time, "monotonic", lambda: now + clients._CACHE_TTL + 1)
    assert clients.lookup("sess-2") is None


def test_session_cache_stays_bounded():
    info = clients.ClientInfo(name="codex", raw="codex", version="0")
    for i in range(clients._CACHE_MAX + 50):
        clients.remember(f"sess-{i}", info)
    assert len(clients._sessions) <= clients._CACHE_MAX


# ── through the middleware ────────────────────────────────────────────────────


class _Inner:
    """Inner app that records the caller context and mints a session id."""

    def __init__(self, session_id=None):
        self.session_id = session_id
        self.seen = None

    async def __call__(self, scope, receive, send):
        client = current_client.get(None)
        self.seen = (
            client.name if client else None,
            current_origin.get(),
            current_session.get(),
        )
        # Drain the body so the replay wrapper is genuinely exercised.
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        headers = []
        if self.session_id:
            headers.append((b"mcp-session-id", self.session_id.encode()))
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"ok"})


class _Send:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)


def _body_receive(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _scope(headers, method="POST"):
    return {
        "type": "http",
        "method": method,
        "path": f"{PREFIX}/mcp",
        "headers": headers,
    }


@pytest.fixture
def authenticated(monkeypatch):
    async def accept(token):
        return AccessToken(token=token, client_id="alice", scopes=[])

    monkeypatch.setattr(server, "verify_token", accept)


AUTH = (b"authorization", b"Bearer good")
IN_CLUSTER = (b"host", b"agentic-interface.cms.svc.cluster.local:8888")
PUBLIC = (b"host", b"cms.geddes.rcac.purdue.edu")


def _sessions_counter(client: str, origin: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "purdue_af_mcp_sessions_total", {"client": client, "origin": origin}
        )
        or 0.0
    )


async def test_handshake_is_counted_and_the_session_remembered(authenticated):
    before = _sessions_counter("claude-code", "in_session")
    inner = _Inner(session_id="abc123def456")

    await server._AuthMiddleware(inner)(
        _scope([AUTH, IN_CLUSTER]), _body_receive(_initialize()), _Send()
    )

    assert inner.seen == ("claude-code", "in_session", "")
    assert _sessions_counter("claude-code", "in_session") == before + 1
    remembered = clients.lookup("abc123def456")
    assert remembered is not None and remembered.name == "claude-code"


async def test_later_calls_inherit_the_client_from_the_session_id(authenticated):
    """The whole point of the cache: tools/call carries no clientInfo."""
    clients.remember(
        "sess-9", clients.ClientInfo(name="codex", raw="codex", version="0.153.4")
    )
    inner = _Inner()
    body = b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"x"}}'

    await server._AuthMiddleware(inner)(
        _scope([AUTH, IN_CLUSTER, (b"mcp-session-id", b"sess-9")]),
        _body_receive(body),
        _Send(),
    )

    # The session prefix is truncated: the full id is a live capability.
    assert inner.seen == ("codex", "in_session", "sess-9"[:8])


async def test_a_known_session_skips_body_parsing(authenticated, monkeypatch):
    """Once identified, a session must not pay to have every body re-read."""
    clients.remember(
        "sess-8", clients.ClientInfo(name="codex", raw="codex", version="0")
    )

    def explode(_body):  # pragma: no cover - must never run
        raise AssertionError("initialize parsed on an already-identified session")

    monkeypatch.setattr(clients, "client_from_initialize", explode)

    await server._AuthMiddleware(_Inner())(
        _scope([AUTH, IN_CLUSTER, (b"mcp-session-id", b"sess-8")]),
        _body_receive(b'{"jsonrpc":"2.0","id":2,"method":"tools/call"}'),
        _Send(),
    )


async def test_external_handshake_is_labelled_external(authenticated):
    before = _sessions_counter("claude-code", "external")

    await server._AuthMiddleware(_Inner(session_id="ext-1"))(
        _scope([AUTH, PUBLIC, (b"x-forwarded-for", b"10.1.2.3")]),
        _body_receive(_initialize()),
        _Send(),
    )

    assert _sessions_counter("claude-code", "external") == before + 1


async def test_failed_handshake_is_not_counted(authenticated):
    """A 4xx initialize never established a session; it must not be recorded."""

    class Failing(_Inner):
        async def __call__(self, scope, receive, send):
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    before = _sessions_counter("claude-code", "in_session")
    await server._AuthMiddleware(Failing())(
        _scope([AUTH, IN_CLUSTER]), _body_receive(_initialize()), _Send()
    )
    assert _sessions_counter("claude-code", "in_session") == before


async def test_delete_forgets_the_session(authenticated):
    clients.remember(
        "sess-7", clients.ClientInfo(name="codex", raw="codex", version="0")
    )
    await server._AuthMiddleware(_Inner())(
        _scope([AUTH, IN_CLUSTER, (b"mcp-session-id", b"sess-7")], method="DELETE"),
        _body_receive(b""),
        _Send(),
    )
    assert clients.lookup("sess-7") is None


async def test_oversized_body_is_refused_before_parsing(authenticated):
    send = _Send()
    huge = b"x" * (server.MAX_BODY_BYTES + 1)
    await server._AuthMiddleware(_Inner())(
        _scope([AUTH, IN_CLUSTER]), _body_receive(huge), send
    )
    assert send.messages[0]["status"] == 413


async def test_unidentifiable_caller_falls_back_to_the_user_agent(authenticated):
    inner = _Inner()
    await server._AuthMiddleware(inner)(
        _scope([AUTH, PUBLIC, (b"user-agent", b"curl/8.4.0")]),
        _body_receive(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}'),
        _Send(),
    )
    assert inner.seen == ("curl", "external", "")
