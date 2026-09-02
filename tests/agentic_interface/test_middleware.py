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


async def test_unprefixed_metrics_is_served():
    # Prometheus scrapes the pod directly at the unprefixed path.
    send = SendCollector()
    await server._AuthMiddleware(RecordingApp())(
        http_scope("/metrics"), noop_receive, send
    )
    assert send.status == 200
    assert b"purdue_af_mcp_api_calls_total" in send.body


async def test_prefixed_metrics_is_404():
    # The proxied form is publicly reachable and the metric families carry
    # username labels — it must not be served.
    inner = RecordingApp()
    send = SendCollector()

    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/metrics"), noop_receive, send
    )

    assert send.status == 404
    assert inner.calls == 0


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


async def test_websocket_scope_is_closed_not_forwarded():
    # Streamable HTTP never uses websockets — the middleware must refuse the
    # handshake rather than send http.response messages on a websocket scope.
    inner = RecordingApp()
    send = SendCollector()
    scope = {"type": "websocket", "path": f"{PREFIX}/mcp", "headers": []}

    await server._AuthMiddleware(inner)(scope, noop_receive, send)

    assert send.messages == [{"type": "websocket.close"}]
    assert inner.calls == 0


async def test_oversized_post_body_is_413(accept_alice, monkeypatch):
    monkeypatch.setattr(server, "MAX_BODY_BYTES", 64)
    chunks = [
        {"type": "http.request", "body": b"x" * 50, "more_body": True},
        {"type": "http.request", "body": b"y" * 50, "more_body": True},
        # never reached — buffering must stop once the cap is exceeded
        {"type": "http.request", "body": b"z" * 50, "more_body": False},
    ]

    async def receive():
        return chunks.pop(0)

    inner = RecordingApp()
    send = SendCollector()
    scope = {**http_scope(f"{PREFIX}/mcp", headers=bearer("good")), "method": "POST"}

    from prometheus_client import REGISTRY

    def counter_413():
        return (
            REGISTRY.get_sample_value(
                "purdue_af_mcp_api_calls_total", {"route": "mcp", "status": "413"}
            )
            or 0.0
        )

    before = counter_413()
    await server._AuthMiddleware(inner)(scope, receive, send)

    assert send.status == 413
    assert json.loads(send.body)["error"] == "Request body too large"
    assert inner.calls == 0
    assert len(chunks) == 1  # the third chunk was never drained
    assert counter_413() == before + 1


async def test_respond_emits_valid_json_for_details_with_quotes():
    send = SendCollector()

    await server._AuthMiddleware._respond(send, 400, 'detail with "quotes"')

    assert json.loads(send.body)["error"] == 'detail with "quotes"'


# ── authentication diagnoses ──────────────────────────────────────────────────
#
# A refused MCP client shows the user nothing but this body, so it must say
# what was wrong with the credential and what to do — and it must not blame
# the token for a hub that could not be asked.


def _headers(send):
    return {k.decode().lower(): v.decode() for k, v in send.messages[0]["headers"]}


def _body(send):
    return json.loads(send.body)


@pytest.fixture
def never_resolve(monkeypatch):
    async def fail(token):
        raise AssertionError(f"resolve_user must not be called for {token!r}")

    monkeypatch.setattr(server, "resolve_user", fail)


async def test_missing_token_carries_a_hint_and_a_bare_challenge():
    send = SendCollector()
    await server._AuthMiddleware(RecordingApp())(
        http_scope(f"{PREFIX}/mcp"), noop_receive, send
    )
    body = _body(send)
    assert body["error"] == "Missing Bearer token"
    assert "/hub/token" in body["hint"]
    assert (
        _headers(send)["www-authenticate"]
        == 'Bearer realm="purdue-af-agentic-interface"'
    )


async def test_empty_token_is_diagnosed_without_asking_the_hub(never_resolve):
    inner = RecordingApp()
    send = SendCollector()
    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/mcp", headers=bearer("")), noop_receive, send
    )
    assert send.status == 401
    body = _body(send)
    assert body["error"] == "Empty Bearer token"
    assert "empty or unset" in body["hint"]
    assert 'error="invalid_token"' in _headers(send)["www-authenticate"]
    assert inner.calls == 0


@pytest.mark.parametrize(
    "literal",
    [
        "${JUPYTERHUB_API_TOKEN}",
        "$JUPYTERHUB_TOKEN",
        "%TOKEN%",
        "<your-api-token>",
        "YOUR_TOKEN",
        "your-api-token-here",
        "...",
    ],
)
async def test_unexpanded_placeholder_is_diagnosed(never_resolve, literal):
    send = SendCollector()
    await server._AuthMiddleware(RecordingApp())(
        http_scope(f"{PREFIX}/mcp", headers=bearer(literal)), noop_receive, send
    )
    assert send.status == 401
    body = _body(send)
    assert body["error"] == "Unexpanded token placeholder"
    assert literal in body["hint"]
    assert "never replaced" in body["hint"]


@pytest.mark.parametrize("token", ["abc123", "a1b2c3d4e5f6a7b8", "tok-with-dashes"])
def test_real_looking_tokens_are_not_placeholders(token):
    assert server._token_problem(token) is None


async def test_wrong_scheme_is_diagnosed(never_resolve):
    send = SendCollector()
    headers = [(b"authorization", b"token abc123")]
    await server._AuthMiddleware(RecordingApp())(
        http_scope(f"{PREFIX}/mcp", headers=headers), noop_receive, send
    )
    assert send.status == 401
    body = _body(send)
    assert body["error"] == "Unsupported Authorization scheme"
    assert "'token'" in body["hint"]


async def test_bearer_scheme_is_case_insensitive(accept_alice):
    inner = RecordingApp()
    headers = [(b"authorization", b"bearer good"), (b"host", b"hub:9999")]
    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/mcp", headers=headers), noop_receive, SendCollector()
    )
    assert inner.calls == 1


async def test_invalid_token_hint_says_how_to_recover(monkeypatch):
    async def reject(token):
        return None

    monkeypatch.setattr(server, "resolve_user", reject)
    send = SendCollector()
    await server._AuthMiddleware(RecordingApp())(
        http_scope(f"{PREFIX}/mcp", headers=bearer("bad")), noop_receive, send
    )
    body = _body(send)
    assert body["error"] == "Invalid JupyterHub token"
    assert "/hub/token" in body["hint"]
    assert "JUPYTERHUB_API_TOKEN" in body["hint"]
    challenge = _headers(send)["www-authenticate"]
    assert 'error="invalid_token"' in challenge
    assert "error_description=" in challenge


async def test_hub_unavailable_is_503_not_401(monkeypatch):
    from auth import HubUnavailable
    from prometheus_client import REGISTRY

    async def down(token):
        raise HubUnavailable("JupyterHub API unreachable — connection refused")

    monkeypatch.setattr(server, "resolve_user", down)

    def counter():
        return (
            REGISTRY.get_sample_value(
                "purdue_af_mcp_api_calls_total", {"route": "mcp", "status": "503"}
            )
            or 0.0
        )

    before = counter()
    inner = RecordingApp()
    send = SendCollector()
    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/mcp", headers=bearer("good")), noop_receive, send
    )

    assert send.status == 503
    body = _body(send)
    assert body["error"] == "JupyterHub API unavailable"
    assert "connection refused" in body["hint"]
    assert "not a token problem" in body["hint"]
    headers = _headers(send)
    assert headers["retry-after"] == "10"
    assert "www-authenticate" not in headers
    assert inner.calls == 0
    assert counter() == before + 1


async def test_unknown_path_hint_names_the_endpoint():
    send = SendCollector()
    await server._AuthMiddleware(RecordingApp())(
        http_scope("/nope"), noop_receive, send
    )
    assert _body(send)["hint"] == f"The MCP endpoint is {PREFIX}/mcp."
