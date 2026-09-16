"""A re-framing proxy in front of Purdue GenAI Studio, on 127.0.0.1.

GenAI Studio's streaming responses (LiteLLM behind the web app) end by
closing the connection without the chunked-encoding terminator. Python's
urllib takes that as end of stream; Node's fetch, which opencode uses,
reports ECONNRESET, so the adapter marks the step failed, retries it with
backoff, and replays every tool call of the step. This proxy reads the
upstream leniently and answers opencode with well-formed responses. GenAI
Studio's rate-limit signal, a JSON null body, becomes an HTTP 429 so the
adapter reports it instead of crashing on it. In practice the limit comes
back as HTTP 400 with the same message; that becomes a 429 too, with a
Retry-After, so the adapter backs off instead of failing the step.

GenAI Studio allows 60 requests a minute per user across every session
that shares the key, so the proxy paces its own requests to SESSION_RPM:
with max_incidents sessions in flight, the sum stays under the limit."""

import http.client
import json
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterator

UPSTREAM = ("genai.rcac.purdue.edu", 443, True)
# 6 sessions x 8/min = 48/min, under the 60/min per user.
SESSION_RPM = 8
RETRY_AFTER_S = 8
RATE_LIMIT_TEXT = b"Rate limit exceeded"
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
            "message": "GenAI Studio rate limit exceeded",
            "type": "rate_limit_error",
        }
    }
).encode()


def _log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] genai-proxy: {message}", flush=True)


class Pacer:
    """At most `rpm` upstream requests a minute, spread evenly: a request
    waits until 60/rpm seconds after the previous one started."""

    def __init__(
        self,
        rpm: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = 60.0 / rpm
        self.clock = clock
        self.sleep = sleep
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> float:
        with self.lock:
            now = self.clock()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        delay = start - now
        if delay > 0:
            self.sleep(delay)
        return delay


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
    pacer = Pacer(SESSION_RPM)

    def do_POST(self) -> None:
        host, port, tls = self.upstream
        waited = self.pacer.wait()
        if waited > 1:
            _log(f"paced {waited:.0f}s before {self.path}")
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
            if resp.status == 200 and first.strip() == b"null":
                _log("rate limited (null body); answering 429")
                self._send(
                    429, RATE_LIMITED, "application/json", retry_after=RETRY_AFTER_S
                )
                return
            if resp.status >= 400:
                body = first + b"".join(pieces)
                if RATE_LIMIT_TEXT in body:
                    _log(f"rate limited (HTTP {resp.status}); answering 429")
                    self._send(
                        429, RATE_LIMITED, "application/json", retry_after=RETRY_AFTER_S
                    )
                    return
                _log(f"upstream HTTP {resp.status} on {self.path}: {body[:400]!r}")
                self._send(
                    resp.status,
                    body,
                    resp.getheader("Content-Type") or "application/json",
                )
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

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        retry_after: int | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


class Proxy:
    """`with Proxy() as p: ... p.url`; serves on a free localhost port."""

    def __init__(
        self, upstream: tuple[str, int, bool] = UPSTREAM, rpm: int = SESSION_RPM
    ) -> None:
        handler = type(
            "BoundHandler", (Handler,), {"upstream": upstream, "pacer": Pacer(rpm)}
        )
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
