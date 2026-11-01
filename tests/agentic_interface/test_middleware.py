"""Tests for the ASGI auth shim in server.py (_AuthMiddleware)."""

import json
from unittest.mock import AsyncMock

import pytest
import server
from agentic_helpers import SendCollector, counter, http_scope, noop_receive
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


def bearer(token):
    return [(b"authorization", f"Bearer {token}".encode()), (b"host", b"hub:9999")]


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


async def test_unknown_path_is_404_and_names_the_endpoint():
    send = SendCollector()
    await server._AuthMiddleware(RecordingApp())(
        http_scope("/anything-else"), noop_receive, send
    )
    assert send.status == 404
    assert _body(send)["hint"] == f"The MCP endpoint is {PREFIX}/mcp."


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


async def test_missing_token_is_401_with_a_hint_and_a_bare_challenge():
    inner = RecordingApp()
    send = SendCollector()
    before = _api_calls("401")

    await server._AuthMiddleware(inner)(http_scope(f"{PREFIX}/mcp"), noop_receive, send)

    assert send.status == 401
    body = _body(send)
    assert body["error"] == "Missing Bearer token"
    assert "/hub/token" in body["hint"]
    assert (
        _headers(send)["www-authenticate"]
        == 'Bearer realm="purdue-af-agentic-interface"'
    )
    assert inner.calls == 0
    assert _api_calls("401") == before + 1


async def test_invalid_token_is_401_and_says_how_to_recover(accept_token):
    inner = RecordingApp()
    send = SendCollector()

    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/mcp", headers=bearer("bad")), noop_receive, send
    )

    assert send.status == 401
    body = _body(send)
    assert body["error"] == "Invalid JupyterHub token"
    assert "/hub/token" in body["hint"]
    assert "JUPYTERHUB_API_TOKEN" in body["hint"]
    challenge = _headers(send)["www-authenticate"]
    assert 'error="invalid_token"' in challenge
    assert "error_description=" in challenge
    assert inner.calls == 0


async def test_valid_token_reaches_inner_app(accept_token):
    inner = RecordingApp()
    send = SendCollector()

    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/mcp", headers=bearer("good")), noop_receive, send
    )

    assert inner.calls == 1
    assert send.status == 200


async def test_user_context_is_bound_during_request_and_reset_after(accept_token):
    inner = RecordingApp()

    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/mcp", headers=bearer("good")),
        noop_receive,
        SendCollector(),
    )

    assert inner.user_during_request == accept_token
    assert current_user.get(None) is None  # reset once the request is done


async def test_host_header_is_rewritten_for_dns_rebinding_protection(accept_token):
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


def _api_calls(status):
    return counter("purdue_af_mcp_api_calls_total", route="mcp", status=status)


@pytest.fixture
def never_resolve(monkeypatch):
    verify = AsyncMock()
    monkeypatch.setattr(server, "verify_token", verify)
    yield
    verify.assert_not_awaited()


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


async def test_bearer_scheme_is_case_insensitive(accept_token):
    inner = RecordingApp()
    headers = [(b"authorization", b"bearer good"), (b"host", b"hub:9999")]
    await server._AuthMiddleware(inner)(
        http_scope(f"{PREFIX}/mcp", headers=headers), noop_receive, SendCollector()
    )
    assert inner.calls == 1


async def test_hub_unavailable_is_503_not_401(monkeypatch):
    from auth import HubUnavailable

    async def down(token):
        raise HubUnavailable("JupyterHub API unreachable — connection refused")

    monkeypatch.setattr(server, "verify_token", down)
    before = _api_calls("503")
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
    assert _api_calls("503") == before + 1


# ── process entry point ───────────────────────────────────────────────────────


def _access_record(message):
    import logging

    return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, message, (), None)


def test_access_log_keeps_only_mcp_requests():
    """Probes and metrics scrapes would otherwise drown the MCP traffic."""
    keep = server._McpAccessFilter().filter
    assert keep(_access_record(f'"POST {PREFIX}/mcp HTTP/1.1" 200'))
    assert not keep(_access_record(f'"GET {PREFIX}/health HTTP/1.1" 200'))


def test_main_serves_the_mcp_app_behind_auth(monkeypatch):
    import logging

    served = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
    monkeypatch.setattr(
        server.uvicorn, "run", lambda app, **kw: served.update(app=app, **kw)
    )
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "filters", [])

    server.main()

    assert isinstance(served["app"], server._AuthMiddleware)
    assert (served["host"], served["port"]) == ("0.0.0.0", 8888)
    assert [type(f) for f in access.filters] == [server._McpAccessFilter]
