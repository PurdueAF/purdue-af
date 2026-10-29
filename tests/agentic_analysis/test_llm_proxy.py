"""apps/agentic-analysis/llm_proxy.py against a fake upstream that streams and
breaks off the way NRP's gateway does, and its wiring to the app's manifests and
models.toml."""

import http.server
import json
import socket
import threading
import time

import httpx
import pytest
import yaml
from common import REPO, load_script, mount_configmap

APP = REPO / "apps" / "agentic-analysis"
EXPERIMENTAL = REPO / "flux" / "experimental" / "kustomization.yaml"
llm = load_script(APP / "llm_proxy.py", "llm_proxy")
TOKEN = "stand-in-token"

CONFIG = """
[proxy]
upstream = "{upstream}"
port = 0
max_body_bytes = {max_body}
bytes_per_token = 3.0
keepalive_seconds = 0.05
hold_hours = 0.01
stream_seconds = 1080
cut_seconds = 0.2
backoff_seconds = 0.01
backoff_max_seconds = 0.05
cut_retries = 2

[models.m]
context = 1_000_000
slots = [2, 4]
budget = 300_000
output_per_minute = 1_000_000
max_tokens = [100, 1_000]
image_tokens = 0
ttft = [1.0, 2.0]
decode = [100.0, 50.0]
variants.high = {{ chat_template_kwargs = {{ reasoning_effort = "high" }} }}

[models.v]
context = 1_000_000
slots = [1, 1]
budget = 300_000
output_per_minute = 1_000_000
max_tokens = [100, 1_000]
image_tokens = 500
ttft = [1.0, 2.0]
decode = [100.0, 50.0]

[roles.worker]
backends = ["m#high", "v"]
context = 100_000
compact_at = 80_000
images = 0
class = 2

[roles.looker]
backends = ["v"]
context = 100_000
compact_at = 80_000
images = 2
class = 1
"""


def _config(upstream="http://u", max_body=200_000):
    return CONFIG.format(upstream=upstream, max_body=max_body)


def _docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _chunk(delta=None, finish=None, usage=None):
    chunk = {"id": "c", "object": "chat.completion.chunk", "model": "m"}
    chunk["choices"] = (
        [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]
    )
    if usage:
        chunk["usage"] = usage
    return chunk


SAID = [
    _chunk({"role": "assistant", "content": "h"}),
    _chunk({"content": "i"}, finish="stop"),
    _chunk(usage={"prompt_tokens": 10, "completion_tokens": 2}),
]


class Upstream:
    """A scripted NRP: each request takes the next scripted answer. Streams are
    chunked; a cut drops the connection inside the body, with no last chunk."""

    def __init__(self):
        self.script, self.bodies = [], []
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                upstream.bodies.append(body)
                self.close_connection = True
                upstream.script.pop(0)(self)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def stream(self, chunks, done=True, delay=0.0, hold=0.0):
        def answer(handler):
            time.sleep(delay)
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            lines = [b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks]
            if done:
                lines.append(b"data: [DONE]\n\n")
            for line in lines:
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
            handler.wfile.flush()
            if done:
                handler.wfile.write(b"0\r\n\r\n")
            else:
                time.sleep(hold)
                handler.connection.shutdown(socket.SHUT_RDWR)

        self.script.append(answer)

    def status(self, code, headers=(), body=b"{}"):
        def answer(handler):
            handler.send_response(code)
            for name, value in headers:
                handler.send_header(name, value)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        self.script.append(answer)

    def whole(self, message, finish):
        reply = {
            "id": "w",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        self.status(200, body=json.dumps(reply).encode())


@pytest.fixture
def upstream():
    fake = Upstream()
    yield fake
    fake.server.shutdown()


def _serve(tmp_path, text):
    path = tmp_path / "models.toml"
    path.write_text(text)
    running = llm.Proxy(llm.load(path), "nrp-key", TOKEN)
    server = llm.Server(("127.0.0.1", 0), running)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    running.url = f"http://127.0.0.1:{server.server_address[1]}"
    return running, server


@pytest.fixture
def proxy(tmp_path, upstream):
    running, server = _serve(tmp_path, _config(upstream.url))
    yield running
    server.shutdown()


def _ask(proxy, body, token=TOKEN, path="/v1/chat/completions"):
    headers = {"Authorization": f"Bearer {token}"}
    return httpx.post(proxy.url + path, json=body, headers=headers, timeout=30)


def _events(response):
    return [
        line[6:] for line in response.text.splitlines() if line.startswith("data: ")
    ]


def _error(event):
    """What OpenCode reads of an error inside a stream: the type and code that
    its message carries."""
    return json.loads(json.loads(event)["error"]["message"])


ASK = {
    "model": "worker",
    "stream": True,
    "messages": [{"role": "user", "content": "hi"}],
}
BAD_CALL = {
    "index": 0,
    "id": "t1",
    "function": {"name": "bash", "arguments": '{"command": "ls'},
}
GOOD_CALL = {
    "id": "t2",
    "type": "function",
    "function": {"name": "bash", "arguments": '{"command": "ls"}'},
}


# --- config ---


def test_every_role_of_the_repositorys_models_fits_its_backends_within_fair_use():
    config = llm.load(APP / "models.toml")
    # Bytes per token of OpenCode's requests at their most, against the estimate's divisor.
    tokens_per_token = 4.4 / config.settings.bytes_per_token
    assert config.roles
    for role in config.roles.values():
        assert role.compact_at <= role.context
        for backend in role.backends:
            model = config.models[backend.model]
            prompt = role.context * tokens_per_token + role.images * model.image_tokens
            assert prompt + model.max_tokens[1] < llm.SHARE * model.context, (
                role.name,
                str(backend),
            )
    assert config.settings.max_body_bytes < 4 * 2**20


@pytest.mark.parametrize(
    "role, complaint",
    [
        ('backends = ["v"]\nimages = 2', None),
        ('backends = ["m"]\nimages = 2', "takes no images"),
        ('backends = ["m#max"]\nimages = 0', "no variant"),
        ('backends = ["x"]\nimages = 0', "no model"),
    ],
)
def test_a_role_names_only_models_and_variants_that_can_serve_it(
    tmp_path, role, complaint
):
    models = _config().split("[roles.worker]")[0]
    path = tmp_path / "models.toml"
    path.write_text(
        models + f"[roles.r]\n{role}\ncontext = 100\ncompact_at = 80\nclass = 0\n"
    )
    if complaint is None:
        assert "r" in llm.load(path).roles
    else:
        with pytest.raises(llm.ConfigError, match=complaint):
            llm.load(path)


def test_the_proxy_will_not_start_without_a_client_token(tmp_path):
    path = tmp_path / "models.toml"
    path.write_text(_config())

    with pytest.raises(llm.ConfigError, match="token"):
        llm.Proxy(llm.load(path), "nrp-key", "")


# --- requests ---


def test_a_streamed_reply_reaches_the_agent_as_the_model_sent_it(proxy, upstream):
    upstream.stream(SAID)

    response = _ask(proxy, {**ASK, "max_tokens": 50_000, "max_completion_tokens": 7})

    events = _events(response)
    assert events[-1] == "[DONE]"
    assert [json.loads(e) for e in events[:-1]] == SAID
    (sent,) = upstream.bodies
    assert sent["model"] == "m"
    assert sent["chat_template_kwargs"] == {"reasoning_effort": "high"}
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    assert sent["max_tokens"] == 1_000 and "max_completion_tokens" not in sent
    assert proxy.gates["m"].status()["output_last_minute"] == 2


def test_a_whole_reply_is_answered_whole(proxy, upstream):
    upstream.stream(SAID)

    response = _ask(proxy, {**ASK, "stream": False})

    assert response.json()["choices"][0]["message"]["content"] == "hi"
    assert response.json()["usage"]["completion_tokens"] == 2


def test_only_the_bearer_of_the_token_is_served(proxy, upstream):
    assert _ask(proxy, ASK, token="wrong").status_code == 401
    assert httpx.get(proxy.url + "/v1/models").status_code == 401
    assert httpx.get(proxy.url + "/healthz").text == "ok"
    assert upstream.bodies == []


def test_requests_too_big_for_the_models_are_refused_as_a_context_overflow(
    proxy, upstream
):
    picture = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    looks = {**ASK, "model": "looker"}

    crowded = _ask(
        proxy, {**looks, "messages": [{"role": "user", "content": [picture] * 3}]}
    )
    huge = _ask(
        proxy, {**ASK, "messages": [{"role": "user", "content": "x" * 300_000}]}
    )
    unknown = _ask(proxy, {**ASK, "model": "nobody"})

    assert crowded.status_code == huge.status_code == 413
    for refused in (crowded, huge):
        assert refused.json()["error"]["code"] == "context_length_exceeded"
    assert unknown.status_code == 400
    assert upstream.bodies == []


def test_a_prompt_over_the_fair_use_share_is_refused_before_the_stream_starts(
    tmp_path, upstream
):
    running, server = _serve(tmp_path, _config(upstream.url, max_body=3_000_000))

    long = {**ASK, "messages": [{"role": "user", "content": "x" * 1_100_000}]}
    response = _ask(running, long)
    server.shutdown()

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "context_length_exceeded"
    assert upstream.bodies == []


def test_a_reply_cut_off_at_the_limit_is_asked_for_again_with_less_output(
    proxy, upstream
):
    upstream.stream(
        [_chunk({"content": "x"}) for _ in range(500)], done=False, hold=0.25
    )
    upstream.stream(SAID)

    response = _ask(proxy, ASK)

    assert [json.loads(e) for e in _events(response)[:-1]] == SAID
    first, second = upstream.bodies
    assert second["max_tokens"] == int(0.8 * 500) < first["max_tokens"]
    assert proxy.gates["m"].failures == 0 and proxy.gates["m"].slots == 2


def test_a_reply_cut_off_every_time_ends_in_a_retryable_error(proxy, upstream):
    for _ in range(3):
        upstream.stream(SAID[:1], done=False, hold=0.25)

    (event,) = _events(_ask(proxy, ASK))

    assert _error(event) == {
        "type": "error",
        "error": {"code": "server_error", "message": "the model's reply was cut off"},
    }
    assert len(upstream.bodies) == 3


def test_a_stream_that_drops_early_is_retried_after_a_pause_with_the_same_cap(
    proxy, upstream
):
    upstream.stream(SAID[:1], done=False)
    upstream.stream(SAID)

    response = _ask(proxy, ASK)

    assert _events(response)[-1] == "[DONE]"
    first, second = upstream.bodies
    assert first["max_tokens"] == second["max_tokens"]
    assert proxy.gates["m"].slots == 1


def test_tool_arguments_that_do_not_parse_are_asked_for_again_whole(proxy, upstream):
    upstream.stream([_chunk({"tool_calls": [BAD_CALL]}, finish="tool_calls")])
    upstream.whole(
        {
            "role": "assistant",
            "content": None,
            "reasoning": "r",
            "tool_calls": [GOOD_CALL],
        },
        "tool_calls",
    )

    events = [json.loads(e) for e in _events(_ask(proxy, ASK))[:-1]]

    assert upstream.bodies[1]["stream"] is False
    assert "stream_options" not in upstream.bodies[1]
    delta = events[0]["choices"][0]["delta"]
    (call,) = delta["tool_calls"]
    assert json.loads(call["function"]["arguments"]) == {"command": "ls"}
    assert call["index"] == 0
    assert delta["reasoning"] == "r"
    assert events[1]["choices"][0]["finish_reason"] == "tool_calls"


def test_when_the_whole_retry_fails_the_agent_gets_the_streamed_reply(proxy, upstream):
    broken = [_chunk({"tool_calls": [BAD_CALL]}, finish="tool_calls")]
    upstream.stream(broken)
    upstream.status(500)

    events = [json.loads(e) for e in _events(_ask(proxy, ASK))[:-1]]

    assert events == broken
    assert len(upstream.bodies) == 2 and proxy.gates["m"].failures == 0


def test_arguments_cut_by_the_output_limit_are_not_asked_for_again(proxy, upstream):
    upstream.stream([_chunk({"tool_calls": [BAD_CALL]}, finish="length")])

    _ask(proxy, ASK)

    assert len(upstream.bodies) == 1


def test_a_rate_limit_is_waited_out_as_told_with_fewer_slots(proxy, upstream):
    upstream.status(429, headers=[("Retry-After", "1")])
    upstream.stream(SAID)
    started = time.monotonic()

    response = _ask(proxy, ASK)

    assert _events(response)[-1] == "[DONE]"
    assert time.monotonic() - started >= 1
    assert proxy.gates["m"].slots == 1


def test_a_server_error_is_retried_after_a_pause(proxy, upstream):
    upstream.status(500)
    upstream.status(503)
    upstream.stream(SAID)

    assert _events(_ask(proxy, ASK))[-1] == "[DONE]"
    assert len(upstream.bodies) == 3
    assert proxy.gates["m"].failures == 0


def test_a_request_the_model_refuses_is_passed_back_in_the_code_that_fits(
    proxy, upstream
):
    too_long = b'{"error": {"message": "maximum context length exceeded"}}'
    upstream.status(400, body=too_long)
    whole = _ask(proxy, {**ASK, "stream": False})
    upstream.status(400, body=b'{"error": {"message": "unknown parameter"}}')
    streamed = _ask(proxy, ASK)

    assert whole.status_code == 400
    assert whole.json()["error"]["code"] == "context_length_exceeded"
    assert _error(_events(streamed)[0])["error"]["code"] == "invalid_prompt"


def test_a_waiting_request_keeps_its_stream_alive(proxy, upstream):
    proxy.gates["m"].slots = 1
    upstream.stream(SAID, delay=0.5)
    upstream.stream(SAID)
    first = threading.Thread(target=_ask, args=(proxy, ASK))
    first.start()
    while proxy.gates["m"].status()["busy"] < 1:
        time.sleep(0.01)

    second = _ask(proxy, ASK)
    first.join()

    assert ": waiting" in second.text
    assert _events(second)[-1] == "[DONE]"


def test_a_request_whose_client_hangs_up_never_reaches_the_model(proxy, upstream):
    proxy.gates["m"].slots = 1
    upstream.stream(SAID, delay=2.0)
    first = threading.Thread(target=_ask, args=(proxy, ASK))
    first.start()
    while proxy.gates["m"].status()["busy"] < 1:
        time.sleep(0.01)
    host, port = proxy.url.removeprefix("http://").split(":")
    body = json.dumps(ASK).encode()
    with socket.create_connection((host, int(port))) as client:
        client.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
            b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        client.recv(64)
    while proxy.gates["m"].status()["waiting"] < 1:
        time.sleep(0.01)

    first.join()
    time.sleep(1.5)

    assert len(upstream.bodies) == 1
    assert proxy.gates["m"].status()["waiting"] == 0


def test_roles_and_their_limits_are_listed_for_the_runner(proxy):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    models = httpx.get(proxy.url + "/v1/models", headers=headers).json()
    config = httpx.get(proxy.url + "/config", headers=headers).json()

    assert {m["id"] for m in models["data"]} == {"worker", "looker"}
    assert config["looker"] == {
        "context": 100_000,
        "compact_at": 80_000,
        "images": 2,
        "max_body_bytes": 200_000,
        "backends": ["v"],
    }


# --- admission ---


def _gate(**overrides):
    fields = dict(
        name="m",
        context=1_000_000,
        slots=(4, 8),
        budget=1_000,
        output_per_minute=10_000,
        max_tokens=(100, 1_000_000),
        image_tokens=0,
        ttft=(1.0, 10.0),
        decode=(100.0, 5.0),
        variants={},
    )
    fields.update(overrides)
    settings = llm.load(APP / "models.toml").settings
    return llm.Gate(llm.Model(**fields), settings)


def _never():
    return False


def test_admission_holds_to_slots_budget_and_output_rate():
    gate = _gate(slots=(2, 2))
    soon = time.monotonic() + 0.2

    assert gate.acquire(600, 0, soon, _never)
    assert not gate.acquire(600, 0, soon, _never)
    assert gate.acquire(300, 0, soon, _never)
    assert not gate.acquire(1, 0, soon, _never)
    gate.release(600, 10_000)
    gate.release(300, 0)
    assert not gate.acquire(1, 0, soon, _never)


def test_one_request_bigger_than_the_budget_runs_alone():
    gate = _gate()

    assert gate.acquire(5_000, 0, time.monotonic() + 0.2, _never)
    assert not gate.acquire(1, 0, time.monotonic() + 0.2, _never)


def test_a_waiting_request_whose_client_is_gone_gives_up_its_place():
    gate = _gate(slots=(1, 1))
    gate.acquire(1, 0, time.monotonic() + 1, _never)
    gone = threading.Event()
    got = []

    def wait():
        got.append(gate.acquire(1, 0, time.monotonic() + 10, gone.is_set))

    waiter = threading.Thread(target=wait)
    waiter.start()
    gone.set()
    waiter.join(3)

    assert got == [False]
    assert gate.status()["waiting"] == 0


def test_the_waiting_request_of_the_first_class_goes_first():
    gate = _gate(slots=(1, 1))
    gate.acquire(1, 0, time.monotonic() + 1, _never)
    order = []

    def wait(rank):
        gate.acquire(1, rank, time.monotonic() + 5, _never)
        order.append(rank)
        gate.release(1, 0)

    late = threading.Thread(target=wait, args=(3,))
    late.start()
    while gate.status()["waiting"] < 1:
        time.sleep(0.01)
    early = threading.Thread(target=wait, args=(0,))
    early.start()
    while gate.status()["waiting"] < 2:
        time.sleep(0.01)
    gate.release(1, 0)
    late.join()
    early.join()

    assert order == [0, 3]


def test_images_count_by_number_not_by_their_bytes(proxy):
    data = "data:image/png;base64," + "A" * 90_000
    picture = {"type": "image_url", "image_url": {"url": data}}
    body = {"model": "looker", "messages": [{"role": "user", "content": [picture] * 2}]}
    raw = json.dumps(body).encode()

    tokens = proxy.estimate(raw, body, proxy.config.models["v"])

    assert 2 * 500 < tokens < 2 * 500 + 100


def test_the_output_cap_follows_the_slow_end_of_recent_speeds():
    gate = _gate()
    assert gate.cap() == int(5.0 * (1080 - 10.0))
    for _ in range(10):
        gate.observe(ttft=4.0, decode=20.0)

    assert gate.cap() == int(20.0 * (1080 - 4.0))


def test_slow_replies_halve_the_slots():
    gate = _gate()
    for _ in range(5):
        gate.observe(ttft=1.0, decode=10.0)

    assert gate.slots == 2


def test_failures_halve_the_slots_at_most_once_in_five_minutes():
    gate = _gate()

    gate.failed(None, 1)
    gate.failed(None, 1)
    assert gate.slots == 2
    assert gate.failed(7.0, 1) == 7.0 and gate.paused_until > time.monotonic()


def test_a_request_backs_off_by_its_own_attempts_and_old_failures_are_forgotten():
    gate = _gate()
    backoff = gate.settings.backoff_seconds

    assert 0.75 * 4 * backoff <= gate.failed(None, 3) <= 1.25 * 4 * backoff
    assert gate.failures == 1
    gate.last_failure -= gate.settings.backoff_max_seconds + 1
    gate.failed(None, 1)
    assert gate.failures == 1


def test_a_session_keeps_its_backend_while_it_is_healthy_and_usable(proxy):
    worker = proxy.config.roles["worker"]

    def anything(backend):
        return True

    def not_m(backend):
        return backend.model != "m"

    assert str(proxy.pick(worker, "s1", anything)) == "m#high"
    proxy.gates["m"].unhealthy_until = time.monotonic() + 60
    assert str(proxy.pick(worker, "s1", anything)) == "v"
    proxy.gates["m"].unhealthy_until = 0
    assert str(proxy.pick(worker, "s1", anything)) == "v"
    assert str(proxy.pick(worker, "s3", anything)) == "m#high"
    assert str(proxy.pick(worker, "s3", not_m)) == "v"
    assert str(proxy.pick(worker, "s4", not_m)) == "v"


def test_the_proxy_exits_once_its_mounted_files_change(tmp_path):
    mount_configmap(tmp_path, "1", {"models.toml": "a"})
    exited = threading.Event()
    watch = threading.Thread(
        target=llm._watch,
        args=([tmp_path / "models.toml"], 0.02, lambda code: exited.set()),
    )
    watch.start()
    time.sleep(0.1)
    assert not exited.is_set()

    mount_configmap(tmp_path, "2", {"models.toml": "b"})

    assert exited.wait(2)
    watch.join(2)
    assert not watch.is_alive()


# --- wiring ---


def test_the_deployment_runs_the_mounted_proxy_with_both_secrets():
    deployment, service = _docs(APP / "llm-proxy.yaml")
    spec = deployment["spec"]["template"]["spec"]
    (container,) = spec["containers"]
    (mount,) = container["volumeMounts"]
    (volume,) = spec["volumes"]
    settings = llm.load(APP / "models.toml").settings

    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert container["command"] == ["python", f"{mount['mountPath']}/llm_proxy.py"]
    assert mount["mountPath"] != "/app"
    assert volume["configMap"]["name"] == deployment["metadata"]["name"]
    refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
    for path, name in (
        ("secret-nrp.yaml", "NRP_API_KEY"),
        ("secret-llm-proxy.yaml", "LLM_PROXY_TOKEN"),
    ):
        (secret,) = _docs(APP / path)
        assert refs[name]["name"] == secret["metadata"]["name"]
        assert refs[name]["key"] in secret["stringData"]
    (job,) = _docs(APP / "workspace.yaml")
    assert (
        container["image"] == job["spec"]["template"]["spec"]["containers"][0]["image"]
    )
    (port,) = container["ports"]
    (exposed,) = service["spec"]["ports"]
    assert port["containerPort"] == settings.port
    assert exposed["targetPort"] == port["name"]
    assert service["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]


def test_flux_generates_the_configmap_from_the_proxy_and_its_models():
    generators = yaml.safe_load(EXPERIMENTAL.read_text())["configMapGenerator"]
    (generator,) = [g for g in generators if g["name"] == "agentic-analysis-llm-proxy"]

    assert sorted(generator["files"]) == [
        "../../apps/agentic-analysis/llm_proxy.py",
        "../../apps/agentic-analysis/models.toml",
    ]
    annotations = generator["options"]["annotations"]
    assert annotations["kustomize.toolkit.fluxcd.io/substitute"] == "disabled"
