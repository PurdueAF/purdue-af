"""The re-framing proxy: a streamed response that the upstream cuts off
without the chunked terminator must reach the client complete and
well-formed; a JSON null body must become a 429."""

import http.client
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


def fake_upstream(body: bytes, chunked_no_terminator: bool):
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
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], seen


def through_proxy(port, path="/api/chat/completions", body=b'{"stream": true}'):
    with genai_proxy.Proxy(upstream=("127.0.0.1", port, False)) as proxy:
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
