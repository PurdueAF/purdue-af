"""Tests for the ASGI shims in server.py: _PathStripper and _AuthMiddleware."""

import json

import pytest
import server
from context import current_user

PREFIX = server.SERVICE_PREFIX  # /services/agentic-interface (default)


class RecordingApp:
    """Inner ASGI app that records the scope and replies 200."""

    def __init__(self):
        self.scope = None
        self.calls = 0
        self.user_during_request = None

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.scope = scope
        self.user_during_request = current_user.get(None)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})


class SendCollector:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return self.messages[0]["status"]

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.messages[1:])


async def noop_receive():  # pragma: no cover - never awaited in these tests
    return {"type": "http.request"}


def http_scope(path, headers=None):
    return {
        "type": "http",
        "path": path,
        "headers": headers or [],
    }


def bearer(token):
    return [(b"authorization", f"Bearer {token}".encode()), (b"host", b"hub:9999")]


# ── _PathStripper ─────────────────────────────────────────────────────────────


async def test_pathstripper_strips_prefix_and_sets_root_path():
    inner = RecordingApp()
    app = server._PathStripper(inner, PREFIX)

    await app(http_scope(f"{PREFIX}/mcp"), noop_receive, SendCollector())

    assert inner.scope["path"] == "/mcp"
    assert inner.scope["root_path"] == PREFIX


async def test_pathstripper_bare_prefix_becomes_root():
    inner = RecordingApp()
    app = server._PathStripper(inner, PREFIX)

    await app(http_scope(PREFIX), noop_receive, SendCollector())

    assert inner.scope["path"] == "/"


async def test_pathstripper_leaves_other_paths_alone():
    inner = RecordingApp()
    app = server._PathStripper(inner, PREFIX)

    await app(http_scope("/other"), noop_receive, SendCollector())

    assert inner.scope["path"] == "/other"


async def test_pathstripper_passes_non_http_scopes_through():
    inner = RecordingApp()
    app = server._PathStripper(inner, PREFIX)
    scope = {"type": "lifespan"}

    await app(scope, noop_receive, SendCollector())

    assert inner.scope is scope


# ── _AuthMiddleware ───────────────────────────────────────────────────────────


async def test_health_is_unauthenticated():
    inner = RecordingApp()
    app = server._AuthMiddleware(inner)
    send = SendCollector()

    await app(http_scope("/health"), noop_receive, send)

    assert send.status == 200
    assert send.body == b"ok"
    assert inner.calls == 0  # answered by the middleware itself


async def test_prefixed_health_is_unauthenticated():
    send = SendCollector()
    await server._AuthMiddleware(RecordingApp())(
        http_scope(f"{PREFIX}/health"), noop_receive, send
    )
    assert send.status == 200


async def test_unknown_path_is_404():
    send = SendCollector()
    await server._AuthMiddleware(RecordingApp())(
        http_scope("/anything-else"), noop_receive, send
    )
    assert send.status == 404


async def test_missing_token_is_401():
    inner = RecordingApp()
    send = SendCollector()

    await server._AuthMiddleware(inner)(http_scope(f"{PREFIX}/mcp"), noop_receive, send)

    assert send.status == 401
    assert json.loads(send.body)["error"] == "Missing Bearer token"
    assert inner.calls == 0


async def test_invalid_token_is_401(monkeypatch):
    async def reject(token):
        return None

    monkeypatch.setattr(server, "resolve_user", reject)
    inner = RecordingApp()
    send = SendCollector()

    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/mcp", headers=bearer("bad")), noop_receive, send
    )

    assert send.status == 401
    assert json.loads(send.body)["error"] == "Invalid JupyterHub token"
    assert inner.calls == 0


@pytest.fixture
def accept_alice(monkeypatch):
    user = {"username": "alice", "namespace": "cms", "token": "t"}

    async def accept(token):
        return user

    monkeypatch.setattr(server, "resolve_user", accept)
    return user


async def test_valid_token_reaches_inner_app(accept_alice):
    inner = RecordingApp()
    send = SendCollector()

    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/mcp", headers=bearer("good")), noop_receive, send
    )

    assert inner.calls == 1
    assert send.status == 200


async def test_user_context_is_bound_during_request_and_reset_after(accept_alice):
    inner = RecordingApp()

    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/mcp", headers=bearer("good")),
        noop_receive,
        SendCollector(),
    )

    assert inner.user_during_request == accept_alice
    assert current_user.get(None) is None  # reset once the request is done


async def test_host_header_is_rewritten_for_dns_rebinding_protection(accept_alice):
    inner = RecordingApp()

    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/mcp", headers=bearer("good")),
        noop_receive,
        SendCollector(),
    )

    headers = dict(inner.scope["headers"])
    assert headers[b"host"] == b"localhost:8888"
    # the authorization header must survive untouched
    assert headers[b"authorization"] == b"Bearer good"


async def test_non_http_scope_passes_through():
    inner = RecordingApp()
    scope = {"type": "lifespan"}

    await server._AuthMiddleware(inner)(scope, noop_receive, SendCollector())

    assert inner.scope is scope
