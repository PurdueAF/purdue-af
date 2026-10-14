"""The re-framing proxy: a streamed response that the upstream cuts off
without the chunked terminator must reach the client complete and
well-formed; a JSON null body must become a 429."""

import email.message
import http.client
import io
import socket
import threading

from common import REPO, load_script

genai_proxy = load_script(
    REPO / "workflows/self-repair/genai_proxy.py", "self_repair_genai_proxy"
)

SSE = (
    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"chatcmpl-tool-1","function":{"name":"read","arguments":"{}"}}]}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    b"data: [DONE]\n\n"
)


def fake_upstream(body: bytes, chunked_no_terminator: bool, status: bytes = b"200 OK"):
    """One-shot HTTP server that answers like LiteLLM at GenAI Studio."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    seen = {}

    def serve():
        conn, _ = srv.accept()
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(65536)
        head, _, rest = request.partition(b"\r\n\r\n")
        seen["head"] = head.decode()
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        while len(rest) < length:
            rest += conn.recv(65536)
        seen["body"] = rest
        if chunked_no_terminator:
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n"
            )
            for piece in (body[: len(body) // 2], body[len(body) // 2 :]):
                conn.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            # no 0\r\n\r\n: the socket just closes, as GenAI Studio does
        else:
            conn.sendall(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], seen


def through_proxy(port, path="/api/chat/completions", body=b'{"stream": true}'):
    with genai_proxy.Proxy(upstream=("127.0.0.1", port, False), rpm=6000) as proxy:
        host, pport = proxy.url.removeprefix("http://").split(":")
        client = http.client.HTTPConnection(host, int(pport), timeout=10)
        client.request(
            "POST",
            path,
            body=body,
            headers={"Authorization": "Bearer k", "Content-Type": "application/json"},
        )
        resp = client.getresponse()
        data = resp.read()  # strict: raises IncompleteRead on a missing terminator
        return resp, data


def test_truncated_chunked_stream_is_delivered_complete_and_terminated():
    port, seen = fake_upstream(SSE, chunked_no_terminator=True)
    resp, data = through_proxy(port)
    assert resp.status == 200
    assert data == SSE, (
        "every byte of the stream, and http.client read it to a clean end"
    )
    assert resp.getheader("Transfer-Encoding") == "chunked"
    assert (
        "Authorization: Bearer k" in seen["head"] and "Host: 127.0.0.1" in seen["head"]
    )
    assert seen["body"] == b'{"stream": true}'


def test_null_body_becomes_a_429():
    port, _ = fake_upstream(b"null", chunked_no_terminator=False)
    resp, data = through_proxy(port, body=b'{"stream": false}')
    assert resp.status == 429
    assert b"rate limit" in data


def test_ordinary_json_passes_through():
    port, _ = fake_upstream(b'{"choices": []}', chunked_no_terminator=False)
    resp, data = through_proxy(port)
    assert resp.status == 200 and data == b'{"choices": []}'


def test_400_rate_limit_becomes_429_with_retry_after():
    port, _ = fake_upstream(
        b'{"detail":"Rate limit exceeded. Please try again later."}',
        False,
        status=b"400 Bad Request",
    )
    resp, data = through_proxy(port, body=b'{"stream": false}')
    assert resp.status == 429
    assert resp.getheader("Retry-After") == str(genai_proxy.RETRY_AFTER_S)
    assert b"rate limit" in data


def test_other_upstream_errors_pass_through_with_their_body():
    port, _ = fake_upstream(
        b'{"detail":"model not found"}', False, status=b"404 Not Found"
    )
    resp, data = through_proxy(port, body=b"{}")
    assert resp.status == 404 and data == b'{"detail":"model not found"}'


def test_pacer_spreads_requests_to_the_budget():
    now = [100.0]
    slept = []
    pacer = genai_proxy.Pacer(rpm=6, clock=lambda: now[0], sleep=slept.append)
    assert pacer.wait() == 0  # the first goes through at once
    assert pacer.wait() == 10  # 60/6
    now[0] += 25  # well past the next slot
    assert pacer.wait() == 0
    assert slept == [10]


def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_unreachable_upstream_is_a_502():
    resp, data = through_proxy(closed_port())
    assert resp.status == 502 and b"upstream:" in data


def test_a_client_gone_before_the_502_is_ignored():
    handler = genai_proxy.Handler.__new__(genai_proxy.Handler)
    handler.upstream = ("127.0.0.1", closed_port(), False)
    handler.pacer = genai_proxy.Pacer(6000)
    handler.path = "/api/chat/completions"
    handler.headers = email.message.Message()
    handler.rfile = io.BytesIO()

    def send(*args, **kwargs):
        raise BrokenPipeError("client went away")

    handler._send = send
    handler.do_POST()


def test_long_pacing_is_logged(capsys):
    port, _ = fake_upstream(b"{}", chunked_no_terminator=False)

    class SlowPacer:
        def wait(self):
            return 5.0

    with genai_proxy.Proxy(upstream=("127.0.0.1", port, False)) as proxy:
        proxy.server.RequestHandlerClass.pacer = SlowPacer()
        host, pport = proxy.url.removeprefix("http://").split(":")
        client = http.client.HTTPConnection(host, int(pport), timeout=10)
        client.request("POST", "/x", body=b"{}")
        assert client.getresponse().read() == b"{}"

    assert "paced 5s before /x" in capsys.readouterr().out


class Cut:
    """An upstream body that fails after `pieces`."""

    def __init__(self, pieces, error):
        self.pieces, self.error = list(pieces), error

    def read(self, size):
        if self.pieces:
            return self.pieces.pop(0)
        raise self.error


def test_a_reset_connection_ends_the_body():
    resp = Cut([b"a"], ConnectionResetError())
    assert list(genai_proxy.read_leniently(resp)) == [b"a"]


def test_an_empty_incomplete_read_adds_nothing():
    resp = Cut([b"a"], http.client.IncompleteRead(b""))
    assert list(genai_proxy.read_leniently(resp)) == [b"a"]
