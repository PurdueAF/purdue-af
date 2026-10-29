"""An OpenAI-compatible proxy for NRP's managed LLMs: it holds the API key,
serves each role of models.toml from that role's NRP models, and keeps every
request within NRP's fair use as models.toml sets out."""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import random
import re
import statistics
import threading
import time
import tomllib
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

CONFIG = Path(
    os.environ.get("LLM_PROXY_CONFIG", str(Path(__file__).with_name("models.toml")))
)
# NRP's fair use: a user's requests in flight to a model hold at most this
# share of its context.
SHARE = 0.35
# Seconds of speed samples that the caps and slots follow.
WINDOW = 900.0
# Seconds a waiting request takes to rise by one queue class.
AGING = 600.0
STICKY = 10_000
DATA_URL = re.compile(rb'"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]*"')
Cancelled = Callable[[], bool]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    upstream: str
    port: int
    max_body_bytes: int
    bytes_per_token: float
    keepalive_seconds: float
    hold_hours: float
    stream_seconds: float
    cut_seconds: float
    backoff_seconds: float
    backoff_max_seconds: float
    cut_retries: int


@dataclass(frozen=True)
class Model:
    name: str
    context: int
    slots: tuple[int, int]
    budget: int
    output_per_minute: int
    max_tokens: tuple[int, int]
    image_tokens: int
    ttft: tuple[float, float]
    decode: tuple[float, float]
    variants: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class Backend:
    model: str
    variant: str

    def __str__(self) -> str:
        return f"{self.model}#{self.variant}" if self.variant else self.model


@dataclass(frozen=True)
class Role:
    name: str
    backends: tuple[Backend, ...]
    context: int
    compact_at: int
    images: int
    rank: int


@dataclass(frozen=True)
class Config:
    settings: Settings
    models: dict[str, Model]
    roles: dict[str, Role]


def load(path: Path) -> Config:
    """models.toml, checked: every backend names a model and variant it has,
    and a role that may send images is served only by models that take them."""
    raw = tomllib.loads(path.read_text())
    settings = Settings(**raw["proxy"])
    models = {}
    for name, m in raw["models"].items():
        model = Model(
            name=name,
            context=m["context"],
            slots=(m["slots"][0], m["slots"][1]),
            budget=m["budget"],
            output_per_minute=m["output_per_minute"],
            max_tokens=(m["max_tokens"][0], m["max_tokens"][1]),
            image_tokens=m["image_tokens"],
            ttft=(m["ttft"][0], m["ttft"][1]),
            decode=(m["decode"][0], m["decode"][1]),
            variants=m.get("variants", {}),
        )
        if not 1 <= model.slots[0] <= model.slots[1]:
            raise ConfigError(f"{name}: slots must be 1 <= start <= max")
        if not 0 < model.max_tokens[0] <= model.max_tokens[1]:
            raise ConfigError(f"{name}: max_tokens must be 0 < floor <= ceiling")
        models[name] = model
    roles = {}
    for name, r in raw["roles"].items():
        backends = []
        for entry in r["backends"]:
            model_name, _, variant = entry.partition("#")
            if model_name not in models:
                raise ConfigError(f"{name}: no model {model_name!r}")
            if variant and variant not in models[model_name].variants:
                raise ConfigError(f"{name}: {model_name} has no variant {variant!r}")
            if r["images"] and not models[model_name].image_tokens:
                raise ConfigError(f"{name}: {model_name} takes no images")
            backends.append(Backend(model_name, variant))
        if not backends:
            raise ConfigError(f"{name}: no backends")
        roles[name] = Role(
            name=name,
            backends=tuple(backends),
            context=r["context"],
            compact_at=r["compact_at"],
            images=r["images"],
            rank=r["class"],
        )
    return Config(settings, models, roles)


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


class Gate:
    """Admission to one model: slots, the tokens its requests in flight hold,
    its output per minute, and how its speed and failures move the slots."""

    def __init__(self, model: Model, settings: Settings) -> None:
        self.model = model
        self.settings = settings
        self.cond = threading.Condition()
        self.slots = model.slots[0]
        self.busy = 0
        self.held = 0
        self.output: deque[tuple[float, int]] = deque()
        self.ttfts: deque[tuple[float, float]] = deque()
        self.decodes: deque[tuple[float, float]] = deque()
        self.waiting: dict[int, tuple[int, float]] = {}
        self.paused_until = 0.0
        self.failures = 0
        self.failing_since: float | None = None
        self.last_failure = float("-inf")
        self.unhealthy_until = 0.0
        self.adjusted = float("-inf")
        self._seq = 0

    def _out(self, now: float) -> int:
        while self.output and now - self.output[0][0] > 60:
            self.output.popleft()
        return sum(n for _, n in self.output)

    def _fits(self, tokens: int, now: float) -> bool:
        return (
            self.busy < self.slots
            and (self.held + tokens <= self.model.budget or self.busy == 0)
            and now >= self.paused_until
            and self._out(now) < self.model.output_per_minute
        )

    def _first(self, seq: int, now: float) -> bool:
        def order(item: tuple[int, tuple[int, float]]) -> tuple[int, float]:
            rank, since = item[1]
            return (max(0, rank - int((now - since) // AGING)), since)

        return min(self.waiting.items(), key=order)[0] == seq

    def acquire(
        self, tokens: int, rank: int, deadline: float, cancelled: Cancelled
    ) -> bool:
        """Waits for room; False once `deadline` passes or the client is gone."""
        with self.cond:
            self._seq += 1
            seq = self._seq
            self.waiting[seq] = (rank, time.monotonic())
            try:
                while True:
                    now = time.monotonic()
                    if cancelled():
                        return False
                    if self._fits(tokens, now) and self._first(seq, now):
                        self.busy += 1
                        self.held += tokens
                        return True
                    if now >= deadline:
                        return False
                    wake = min(1.0, deadline - now)
                    if self.paused_until > now:
                        wake = min(wake, self.paused_until - now)
                    self.cond.wait(timeout=wake)
            finally:
                del self.waiting[seq]
                self.cond.notify_all()

    def release(self, tokens: int, completion: int) -> None:
        with self.cond:
            self.busy -= 1
            self.held -= tokens
            if completion:
                self.output.append((time.monotonic(), completion))
            self.cond.notify_all()

    def cap(self) -> int:
        """max_tokens a reply decodes within `stream_seconds` at the model's
        slow recent speed, or at its configured slow speed until it has some."""
        floor, ceiling = self.model.max_tokens
        with self.cond:
            self._trim(time.monotonic())
            if len(self.decodes) >= 5:
                slow = _quantile([r for _, r in self.decodes], 0.1)
                late = _quantile([t for _, t in self.ttfts], 0.9) if self.ttfts else 0
            else:
                slow, late = self.model.decode[1], self.model.ttft[1]
        within = int(slow * (self.settings.stream_seconds - late))
        return max(floor, min(ceiling, within))

    def _trim(self, now: float) -> None:
        for samples in (self.ttfts, self.decodes):
            while samples and now - samples[0][0] > WINDOW:
                samples.popleft()

    def observe(self, ttft: float | None, decode: float | None) -> None:
        with self.cond:
            now = time.monotonic()
            if ttft is not None:
                self.ttfts.append((now, ttft))
            if decode is not None:
                self.decodes.append((now, decode))
            self._trim(now)
            self._adjust(now)
            self.cond.notify_all()

    def succeeded(self, ttft: float | None, decode: float | None) -> None:
        with self.cond:
            self.failures = 0
            self.failing_since = None
        self.observe(ttft, decode)

    def _adjust(self, now: float) -> None:
        if len(self.decodes) < 5 or now - self.adjusted < 300:
            return
        ttft = statistics.median(t for _, t in self.ttfts) if self.ttfts else 0
        decode = statistics.median(r for _, r in self.decodes)
        if ttft > 3 * self.model.ttft[0] or decode < 0.5 * self.model.decode[0]:
            self.slots = max(1, self.slots // 2)
            self.adjusted = now
        elif now - self.adjusted >= 600 and self.slots < self.model.slots[1]:
            self.slots += 1
            self.adjusted = now

    def failed(self, retry_after: float | None, attempt: int) -> float:
        """Seconds to wait before a request's next try: the model's own
        Retry-After on a 429, otherwise a backoff doubling with the request's
        own `attempt`. Halves the slots at most once in five minutes."""
        with self.cond:
            now = time.monotonic()
            if now - self.last_failure > self.settings.backoff_max_seconds:
                self.failures, self.failing_since = 0, None
            self.failures += 1
            self.last_failure = now
            self.failing_since = self.failing_since or now
            if now - self.adjusted >= 300:
                self.slots = max(1, self.slots // 2)
                self.adjusted = now
            if now - self.failing_since >= 1800:
                self.unhealthy_until = now + 600
            if retry_after is not None:
                self.paused_until = now + retry_after
                return retry_after
            base = self.settings.backoff_seconds * 2.0 ** (attempt - 1)
            return min(self.settings.backoff_max_seconds, base) * random.uniform(
                0.75, 1.25
            )

    def healthy(self) -> bool:
        return time.monotonic() >= self.unhealthy_until

    def status(self) -> dict[str, Any]:
        with self.cond:
            now = time.monotonic()
            return {
                "slots": self.slots,
                "busy": self.busy,
                "held": self.held,
                "waiting": len(self.waiting),
                "output_last_minute": self._out(now),
                "healthy": now >= self.unhealthy_until,
                "failures": self.failures,
            }


@dataclass
class Reply:
    """One upstream answer, kept whole so that it can be checked first."""

    status: int = 0
    payloads: list[bytes] = field(default_factory=list)
    done: bool = False
    finish: str | None = None
    content: str = ""
    reasoning: str = ""
    reasoning_name: str = "reasoning_content"
    calls: dict[int, dict[str, str]] = field(default_factory=dict)
    usage: dict[str, Any] | None = None
    ttft: float | None = None
    seconds: float = 0.0
    error: str = ""
    code: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def take(self, chunk: dict[str, Any]) -> None:
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or choice.get("message") or {}
            self.content += delta.get("content") or ""
            for name in ("reasoning_content", "reasoning"):
                if delta.get(name):
                    self.reasoning += delta[name]
                    self.reasoning_name = name
            for n, call in enumerate(delta.get("tool_calls") or []):
                slot = self.calls.setdefault(
                    call.get("index", n), {"id": "", "name": "", "arguments": ""}
                )
                slot["id"] = call.get("id") or slot["id"]
                function = call.get("function") or {}
                slot["name"] += function.get("name") or ""
                slot["arguments"] += function.get("arguments") or ""
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]

    def completion(self) -> int:
        """Output tokens: as counted, or for a cut reply its chunks, about a token each."""
        return int((self.usage or {}).get("completion_tokens") or len(self.payloads))

    def problem(self) -> str | None:
        """Why this reply must not reach the agent as it is, if it must not."""
        if not self.done:
            return "cut"
        if self.finish == "tool_calls" and not self.calls:
            return "no tool call"
        for call in self.calls.values():
            try:
                json.loads(call["arguments"] or "{}")
            except ValueError:
                return "bad tool arguments"
        return None

    def decode(self) -> float | None:
        tokens = self.completion()
        spent = self.seconds - (self.ttft or 0)
        return tokens / spent if tokens > 50 and spent > 1 else None

    def message(self, role: str) -> dict[str, Any]:
        """The reply as a chat.completion."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.reasoning:
            message[self.reasoning_name] = self.reasoning
        if self.calls:
            message["tool_calls"] = [
                {
                    "id": c["id"] or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                }
                for _, c in sorted(self.calls.items())
            ]
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": role,
            "choices": [{"index": 0, "message": message, "finish_reason": self.finish}],
            "usage": self.usage,
        }

    def chunks(self, role: str) -> list[bytes]:
        """The reply as stream payloads: as received, or rebuilt from a whole one."""
        if self.payloads:
            return self.payloads
        whole = self.message(role)
        delta = dict(whole["choices"][0]["message"])
        for n, call in enumerate(delta.get("tool_calls") or []):
            call["index"] = n
        base = {k: whole[k] for k in ("id", "created", "model")}
        base["object"] = "chat.completion.chunk"
        out = [
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": self.finish}],
            },
        ]
        if self.usage:
            out.append({**base, "choices": [], "usage": self.usage})
        return [json.dumps(c).encode() for c in out]


def failure(status: int, code: str, message: str) -> Reply:
    """An error answered in the codes OpenCode acts on: context_length_exceeded
    compacts the session, server_error is retried, invalid_prompt ends the turn."""
    return Reply(status=status, error=message, code=code)


def error_body(reply: Reply) -> bytes:
    return json.dumps(
        {"error": {"message": reply.error, "type": "proxy", "code": reply.code}}
    ).encode()


def error_chunk(reply: Reply) -> bytes:
    """An error inside a stream. The client hands OpenCode only its message,
    so the message carries the type and code OpenCode reads."""
    inner = json.dumps(
        {"type": "error", "error": {"code": reply.code, "message": reply.error}}
    )
    return json.dumps(
        {"error": {"message": inner, "type": "proxy", "code": reply.code}}
    ).encode()


def _upstream_error(reply: Reply, text: bytes) -> Reply:
    """An upstream refusal, passed on in the code that fits it."""
    message = text.decode(errors="replace")[:2000]
    code = (
        "context_length_exceeded" if "context" in message.lower() else "invalid_prompt"
    )
    reply.error, reply.code = message, code
    return reply


def images(body: dict[str, Any]) -> int:
    count = 0
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            count += sum(1 for part in content if part.get("type") == "image_url")
    return count


class Proxy:
    def __init__(
        self, config: Config, key: str, token: str, client: httpx.Client | None = None
    ) -> None:
        if not token:
            raise ConfigError("the client token is empty")
        self.config = config
        self.key = key
        self.token = token
        self.gates = {
            name: Gate(m, config.settings) for name, m in config.models.items()
        }
        # By role too: a compaction comes with the session of the agent it compacts.
        self.sticky: OrderedDict[tuple[str, str], Backend] = OrderedDict()
        self.lock = threading.Lock()
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=15, read=300, write=60, pool=60),
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
        )

    def estimate(self, raw: bytes, body: dict[str, Any], model: Model) -> int:
        """Prompt tokens of a request: its text by size, its images by count."""
        pictures = sum(len(m.group()) for m in DATA_URL.finditer(raw))
        text = (len(raw) - pictures) / self.config.settings.bytes_per_token
        return int(text) + images(body) * model.image_tokens

    def room(self, prompt: int, backend: Backend) -> int:
        """The most output tokens a request of `prompt` tokens may ask of
        `backend` within fair use."""
        return int(SHARE * self.config.models[backend.model].context) - prompt - 1

    def usable(self, raw: bytes, body: dict[str, Any], backend: Backend) -> bool:
        model = self.config.models[backend.model]
        return (
            self.room(self.estimate(raw, body, model), backend) >= model.max_tokens[0]
        )

    def pick(
        self, role: Role, session: str, usable: Callable[[Backend], bool]
    ) -> Backend:
        """The backend this session last went to in this role while it is
        healthy and usable, else the role's first such one."""
        key = (role.name, session)
        with self.lock:
            kept = self.sticky.get(key) if session else None
            if (
                kept
                and kept in role.backends
                and self.gates[kept.model].healthy()
                and usable(kept)
            ):
                return kept
            candidates = [b for b in role.backends if usable(b)] or list(role.backends)
            chosen = next(
                (b for b in candidates if self.gates[b.model].healthy()), candidates[0]
            )
            if session:
                self.sticky[key] = chosen
                self.sticky.move_to_end(key)
                while len(self.sticky) > STICKY:
                    self.sticky.popitem(last=False)
            return chosen

    def shape(
        self, body: dict[str, Any], backend: Backend, cap: int, stream: bool
    ) -> dict[str, Any]:
        out = {k: v for k, v in body.items() if k != "max_completion_tokens"}
        out["model"] = backend.model
        extra = self.config.models[backend.model].variants.get(backend.variant, {})
        for key, value in extra.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = {**out[key], **value}
            else:
                out[key] = value
        asked = body.get("max_tokens") or body.get("max_completion_tokens") or cap
        out["max_tokens"] = min(int(asked), cap)
        out["stream"] = stream
        if stream:
            out["stream_options"] = {
                **(body.get("stream_options") or {}),
                "include_usage": True,
            }
        else:
            out.pop("stream_options", None)
        return out

    def exchange(self, payload: dict[str, Any], cancelled: Cancelled) -> Reply:
        """One request upstream. A stream that breaks off after its headers
        comes back as a cut reply with what it carried."""
        reply = Reply()
        started = time.monotonic()
        url = self.config.settings.upstream.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.key}"}
        if not payload["stream"]:
            # A whole reply arrives only at its end, which NRP lets run to its cut.
            read = self.config.settings.stream_seconds + 180
            timeout = httpx.Timeout(15, read=read, write=60, pool=60)
            response = self.client.post(
                url, json=payload, headers=headers, timeout=timeout
            )
            reply.status = response.status_code
            reply.headers = dict(response.headers)
            reply.seconds = time.monotonic() - started
            if response.status_code != 200:
                return _upstream_error(reply, response.content)
            reply.take(response.json())
            reply.done = True
            return reply
        with self.client.stream("POST", url, json=payload, headers=headers) as response:
            reply.status = response.status_code
            reply.headers = dict(response.headers)
            if response.status_code != 200:
                reply.seconds = time.monotonic() - started
                return _upstream_error(reply, response.read())
            try:
                for line in response.iter_lines():
                    if cancelled():
                        break
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        reply.done = True
                        break
                    if reply.ttft is None:
                        reply.ttft = time.monotonic() - started
                    reply.payloads.append(data.encode())
                    reply.take(json.loads(data))
            except httpx.TransportError:
                pass
        reply.seconds = time.monotonic() - started
        return reply

    def forward(
        self,
        role: Role,
        raw: bytes,
        body: dict[str, Any],
        session: str,
        cancelled: Cancelled = lambda: False,
    ) -> tuple[Reply, dict[str, Any]]:
        """Sends a request on until it has a reply fit for the agent, or a
        final error. Returns it with a record of what it took."""
        settings = self.config.settings
        deadline = time.monotonic() + settings.hold_hours * 3600
        record: dict[str, Any] = {"role": role.name, "session": session, "attempts": 0}
        cuts, shrink, failures = 0, 0, 0
        streamed: Reply | None = None
        while True:
            if cancelled():
                record["client_gone"] = True
                return failure(499, "server_error", "the client is gone"), record
            if streamed is not None and time.monotonic() >= deadline:
                record["problem"] = streamed.problem()
                return streamed, record
            backend = self.pick(role, session, lambda b: self.usable(raw, body, b))
            gate = self.gates[backend.model]
            model = self.config.models[backend.model]
            prompt = self.estimate(raw, body, model)
            room = self.room(prompt, backend)
            if room < model.max_tokens[0]:
                return failure(
                    413, "context_length_exceeded", "over the models' fair-use size"
                ), record
            cap = min(gate.cap(), room, shrink or room)
            payload = self.shape(body, backend, cap, stream=streamed is None)
            tokens = prompt + payload["max_tokens"]
            record.update(backend=str(backend), max_tokens=payload["max_tokens"])
            queued = time.monotonic()
            if not gate.acquire(tokens, role.rank, deadline, cancelled):
                if cancelled():
                    record["client_gone"] = True
                    return failure(499, "server_error", "the client is gone"), record
                if streamed is not None:
                    record["problem"] = streamed.problem()
                    return streamed, record
                return failure(503, "server_error", "no room within the hold"), record
            record["queued_s"] = record.get("queued_s", 0) + time.monotonic() - queued
            record["attempts"] += 1
            reply = Reply()
            try:
                reply = self.exchange(payload, cancelled)
            except (httpx.HTTPError, ValueError) as failed:
                reply = Reply(status=599, error=type(failed).__name__)
            finally:
                gate.release(tokens, reply.completion() if reply.status == 200 else 0)
            record.update(
                status=reply.status,
                ratelimit_remaining=reply.headers.get("x-ratelimit-remaining"),
            )
            if streamed is not None and (
                reply.status != 200 or reply.problem() == "cut"
            ):
                # The whole retry failed: the agent gets the streamed reply.
                record["problem"] = streamed.problem()
                return streamed, record
            problem = reply.problem() if reply.status == 200 else None
            early = problem == "cut" and reply.seconds < 0.9 * settings.cut_seconds
            if reply.status == 429 or reply.status >= 500 or early:
                # A stream that broke off well before NRP's cut failed; it was not too long.
                if cancelled():
                    continue
                failures += 1
                record["failures"] = failures
                retry = _retry_after(reply) if reply.status == 429 else None
                wait = gate.failed(retry, failures)
                if time.monotonic() + wait >= deadline:
                    return failure(
                        503, "server_error", "the model kept failing"
                    ), record
                time.sleep(wait)
                continue
            if reply.status != 200:
                return reply, record
            if problem == "cut":
                gate.observe(reply.ttft, reply.decode())
                cuts += 1
                record["cuts"] = cuts
                if cuts > settings.cut_retries or time.monotonic() >= deadline:
                    return failure(
                        502, "server_error", "the model's reply was cut off"
                    ), record
                # Less than the cut reply reached, so that the next one ends in time.
                shrink = max(model.max_tokens[0], int(0.8 * reply.completion()))
                continue
            if problem and reply.finish != "length" and streamed is None:
                streamed = reply
                record["reshaped"] = problem
                continue
            gate.succeeded(reply.ttft, reply.decode())
            record.update(
                finish=reply.finish,
                usage=reply.usage,
                ttft=reply.ttft,
                seconds=round(reply.seconds, 3),
                problem=problem,
            )
            return reply, record

    def config_view(self) -> dict[str, Any]:
        """What a runner configures its agents from."""
        return {
            name: {
                "context": r.context,
                "compact_at": r.compact_at,
                "images": r.images,
                "max_body_bytes": self.config.settings.max_body_bytes,
                "backends": [str(b) for b in r.backends],
            }
            for name, r in self.config.roles.items()
        }


def _retry_after(reply: Reply) -> float:
    for name in ("retry-after", "x-ratelimit-reset"):
        try:
            return max(1.0, float(reply.headers[name]))
        except (KeyError, ValueError):
            continue
    return 60.0


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: Server

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _authorized(self) -> bool:
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(given.encode(), self.server.proxy.token.encode())

    def _send(self, status: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self, status: int, code: str, message: str) -> None:
        self._send(status, error_body(failure(status, code, message)))

    def do_GET(self) -> None:
        proxy = self.server.proxy
        if self.path == "/healthz":
            self._send(200, b"ok", "text/plain")
            return
        if not self._authorized():
            self._refuse(401, "invalid_prompt", "a bearer token is required")
            return
        if self.path in ("/v1/models", "/models"):
            data = [{"id": name, "object": "model"} for name in proxy.config.roles]
            self._send(200, json.dumps({"object": "list", "data": data}).encode())
        elif self.path == "/config":
            self._send(200, json.dumps(proxy.config_view()).encode())
        elif self.path == "/status":
            gates = {name: g.status() for name, g in proxy.gates.items()}
            self._send(200, json.dumps(gates).encode())
        else:
            self._refuse(404, "invalid_prompt", "no such path")

    def do_POST(self) -> None:
        proxy = self.server.proxy
        settings = proxy.config.settings
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._refuse(404, "invalid_prompt", "no such path")
            return
        if not self._authorized():
            self._refuse(401, "invalid_prompt", "a bearer token is required")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > settings.max_body_bytes:
            self.rfile.read(min(length, 64 * 2**20))
            self.close_connection = True
            self._refuse(
                413,
                "context_length_exceeded",
                f"a body of {length} bytes is over the {settings.max_body_bytes}"
                " the models accept",
            )
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            self._refuse(400, "invalid_prompt", "the body is not JSON")
            return
        role = proxy.config.roles.get(str(body.get("model")))
        if role is None:
            self._refuse(
                400, "invalid_prompt", f"no role {body.get('model')!r}; see /v1/models"
            )
            return
        sent = images(body)
        if sent > role.images:
            self._refuse(
                413,
                "context_length_exceeded",
                f"{sent} images in one request; {role.name} takes at most {role.images}",
            )
            return
        if not any(proxy.usable(raw, body, b) for b in role.backends):
            self._refuse(
                413, "context_length_exceeded", "over the models' fair-use size"
            )
            return
        session = self.headers.get("x-af-session", "")
        started = time.time()
        if body.get("stream"):
            reply, record = self._stream(role, raw, body, session)
        else:
            reply, record = proxy.forward(role, raw, body, session)
            try:
                if reply.status == 200:
                    self._send(200, json.dumps(reply.message(role.name)).encode())
                else:
                    self._send(reply.status, error_body(reply))
            except OSError:
                record["client_gone"] = True
        record.update(kind="llm", start=started, images=sent, body_bytes=length)
        print(json.dumps(record, default=str), flush=True)

    def _stream(
        self, role: Role, raw: bytes, body: dict[str, Any], session: str
    ) -> tuple[Reply, dict[str, Any]]:
        """Answers at once, keeps the stream alive while the request waits and
        runs, then replays the checked reply."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        lock, stop, gone = threading.Lock(), threading.Event(), threading.Event()

        def write(data: bytes) -> None:
            with lock:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()

        def alive() -> None:
            while not stop.wait(self.server.proxy.config.settings.keepalive_seconds):
                try:
                    write(b": waiting\n\n")
                except OSError:
                    gone.set()
                    return

        ticker = threading.Thread(target=alive, daemon=True)
        ticker.start()
        try:
            reply, record = self.server.proxy.forward(
                role, raw, body, session, gone.is_set
            )
        finally:
            stop.set()
            ticker.join()
        if gone.is_set():
            record["client_gone"] = True
            return reply, record
        try:
            if reply.status == 200:
                for payload in reply.chunks(role.name):
                    write(b"data: " + payload + b"\n\n")
                write(b"data: [DONE]\n\n")
            else:
                write(b"data: " + error_chunk(reply) + b"\n\n")
            with lock:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except OSError:
            record["client_gone"] = True
        return reply, record


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], proxy: Proxy) -> None:
        self.proxy = proxy
        super().__init__(address, Handler)


def _digest(paths: list[Path]) -> str:
    return hashlib.sha256(b"".join(p.read_bytes() for p in paths)).hexdigest()


def _watch(
    paths: list[Path], every: float = 30.0, leave: Callable[[int], object] = os._exit
) -> None:
    """Exits once the mounted code or config changes, for the kubelet to start
    it anew: a ConfigMap update reaches the files but not the running process."""
    seen = _digest(paths)
    while True:
        time.sleep(every)
        if _digest(paths) != seen:
            print(json.dumps({"kind": "restart", "reason": "changed"}), flush=True)
            leave(0)
            return


def main() -> None:
    config = load(CONFIG)
    proxy = Proxy(config, os.environ["NRP_API_KEY"], os.environ["LLM_PROXY_TOKEN"])
    watched = [Path(__file__), CONFIG]
    threading.Thread(target=_watch, args=(watched,), daemon=True).start()
    server = Server(("", config.settings.port), proxy)
    start = {"kind": "start", "port": config.settings.port, "roles": list(config.roles)}
    print(json.dumps(start), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
