"""A re-framing proxy in front of Purdue GenAI Studio, on 127.0.0.1.

GenAI Studio's streaming responses (LiteLLM behind the web app) end by
closing the connection without the chunked-encoding terminator. Python's
urllib takes that as end of stream; Node's fetch, which opencode uses,
reports ECONNRESET, so the adapter marks the step failed, retries it with
backoff, and replays every tool call of the step. This proxy reads the
upstream leniently and answers opencode with well-formed responses. GenAI
Studio's rate-limit signal, a JSON null body, becomes an HTTP 429 so the
adapter reports it instead of crashing on it."""

import http.client
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

UPSTREAM = ("genai.rcac.purdue.edu", 443, True)
HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}
RATE_LIMITED = json.dumps(
    {
        "error": {
            "message": "GenAI Studio rate limit (null response body)",
            "type": "rate_limit_error",
        }
    }
).encode()


def read_leniently(resp: http.client.HTTPResponse) -> Iterator[bytes]:
    """Yield the body as it arrives; a connection cut before the chunked
    terminator is the end of the body, not an error."""
    try:
        while True:
            piece = resp.read(65536)
            if not piece:
                return
            yield piece
    except http.client.IncompleteRead as exc:
        if exc.partial:
            yield exc.partial
    except (ConnectionResetError, socket.timeout, TimeoutError):
        return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = UPSTREAM

    def do_POST(self) -> None:
        host, port, tls = self.upstream
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Host"] = host
        headers["Content-Length"] = str(len(body))
        conn: http.client.HTTPConnection = (
            http.client.HTTPSConnection(host, port, timeout=600)
            if tls
            else http.client.HTTPConnection(host, port, timeout=600)
        )
        try:
            conn.request("POST", self.path, body=body, headers=headers)
            resp = conn.getresponse()
            pieces = read_leniently(resp)
            first = next(pieces, b"")
            if first.strip() in (b"null", b""):
                if resp.status == 200 and first.strip() == b"null":
                    self._send(429, RATE_LIMITED, "application/json")
                    return
            headers_out = [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP]
            self.send_response(resp.status, resp.reason)
            for k, v in headers_out:
                self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in (first, *pieces) if first else pieces:
                if piece:
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            try:
                self._send(
                    502,
                    json.dumps({"error": {"message": f"upstream: {exc}"}}).encode(),
                    "application/json",
                )
            except OSError:
                pass
        finally:
            conn.close()

    do_GET = do_POST

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


class Proxy:
    """`with Proxy() as p: ... p.url`; serves on a free localhost port."""

    def __init__(self, upstream: tuple[str, int, bool] = UPSTREAM) -> None:
        handler = type("BoundHandler", (Handler,), {"upstream": upstream})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "Proxy":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
