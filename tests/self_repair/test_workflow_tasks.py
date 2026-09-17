"""The Flyte tasks of workflows/self-repair, imported against a stub `flyte`
(the SDK is not a test dependency): decorators pass through, and every
cluster, git, GitHub and GenAI call is replaced per test."""

import asyncio
import io
import json
import subprocess
import sys
import threading
import types
import urllib.error
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from common import REPO, load_script

WORKFLOW = REPO / "workflows/self-repair"


def _flyte_stub():
    flyte = types.ModuleType("flyte")

    class TaskEnvironment:
        def __init__(self, **options):
            self.options = options

        def task(self, **options):
            def decorate(fn):
                fn.task_options = options
                return fn

            return decorate

    flyte.TaskEnvironment = TaskEnvironment
    flyte.Resources = lambda **kw: kw
    flyte.Cache = lambda **kw: kw
    flyte.ctx = lambda: None
    flyte.report = SimpleNamespace(replace=SimpleNamespace(aio=None))
    flyte.with_runcontext = None
    return flyte


def _load():
    names = ("flyte", "triage", "prompts", "genai_proxy")
    saved_modules = {name: sys.modules.get(name) for name in names}
    saved_path = list(sys.path)
    sys.modules["flyte"] = _flyte_stub()
    sys.path.insert(0, str(WORKFLOW))
    try:
        return load_script(WORKFLOW / "self_repair.py", "self_repair_flows")
    finally:
        sys.path[:] = saved_path
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


sr = _load()


def incident(fp, count=1, workload="hub", first_pod="hub-1", first_ts=""):
    return sr.Incident(
        sr.IncidentKey(fp, "c", workload, f"Error {fp}"),
        sr.Evidence(
            [f"{first_pod}/c: Error {fp}"],
            count,
            1,
            "2026-09-16T10:00:00+00:00",
            "2026-09-16T10:05:00+00:00",
            first_pod,
            first_ts or "2026-09-16T10:00:00+00:00",
        ),
    )


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body))


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(sr.time, "sleep", slept.append)
    return slept


class TestRunNames:
    """Flyte caps run names at 30 characters; the pod is `<run>-a0-0`."""

    def test_every_run_name_fits(self):
        tick = sr.tick_of(datetime(2084, 1, 1, tzinfo=timezone.utc))
        assert len(tick) == 5
        for task, fp in (
            ("triage", ""),
            ("watch", ""),
            ("analyze", "189fbee7047b"),
            ("fix", "189fbee7047b"),
        ):
            name = sr.run_name(task, tick, fp)
            assert name.startswith(f"self-repair-{task}-{tick}"), name
            assert len(name) <= 30, name
        assert sr.run_name("analyze", tick, "189fbee7047b").endswith("-189f")

    def test_tick_is_the_minute_and_padded(self):
        a = sr.tick_of(datetime(2026, 9, 16, 16, 36, 5, tzinfo=timezone.utc))
        b = sr.tick_of(datetime(2026, 9, 16, 16, 36, 59, tzinfo=timezone.utc))
        c = sr.tick_of(datetime(2026, 9, 16, 16, 37, 0, tzinfo=timezone.utc))
        assert a == b != c and len(a) == 5 and a.islower()
        assert sr.tick_of(datetime(1970, 1, 1, 0, 1, tzinfo=timezone.utc)) == "00001"

    def test_an_overlong_name_is_refused(self):
        with pytest.raises(ValueError, match="longer than 30"):
            sr.run_name("a-much-longer-task", "00001")


class TestGuards:
    def test_protected_paths_block_the_vendored_fork_envs_deploy_and_locks(self):
        blocked = sr._protected(
            [
                "docker/dask-gateway-server/x.py",
                "pixi/global/pixi.toml",
                "deploy/experimental/kustomization.yaml",
                "docker/self-repair/uv.lock",
                "apps/x/values.yaml",
            ]
        )
        assert blocked == [
            "docker/dask-gateway-server/x.py",
            "pixi/global/pixi.toml",
            "deploy/experimental/kustomization.yaml",
            "docker/self-repair/uv.lock",
        ]

    def test_pyflakes_gate_catches_an_undefined_name(self, tmp_path):
        (tmp_path / "ok.py").write_text("import os\nprint(os.name)\n")
        (tmp_path / "bad.py").write_text("logger = 1\nl.handlers = []\n")
        assert sr._python_defects(tmp_path, ["ok.py"]) == ""
        assert "F821" in sr._python_defects(tmp_path, ["ok.py", "bad.py"])

    def test_pyflakes_gate_skips_non_python_and_deleted_files(self, tmp_path):
        (tmp_path / "values.yaml").write_text("a: 1\n")
        assert sr._python_defects(tmp_path, ["values.yaml", "gone.py"]) == ""

    def test_edit_permissions_deny_what_protected_paths_block(self):
        rules = sr.EDIT["edit"]
        assert rules["*"] == "allow" and rules["*.lock"] == "deny"
        assert all(rules[k] == "deny" for k in rules if k != "*")
        assert sr.EDIT["bash"]["git push*"] == "deny"
        assert sr.READ_ONLY["edit"] == sr.READ_ONLY["bash"] == "deny"


class TestGit:
    def test_git_runs_in_the_checkout_and_returns_stdout(self, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        assert sr._git("rev-parse", "--is-inside-work-tree", cwd=tmp_path) == "true\n"
        with pytest.raises(subprocess.CalledProcessError):
            sr._git("no-such-command", cwd=tmp_path)

    def test_clone_keeps_the_token_out_of_urls_and_config(self, monkeypatch, tmp_path):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs.get("cwd")))
            return subprocess.CompletedProcess(argv, 0, stdout="abc1234\n")

        monkeypatch.setattr(sr.subprocess, "run", run)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")

        sr._clone(tmp_path / "repo")

        clone = calls[0][0]
        assert clone[:2] == ["git", "clone"] and "--depth" in clone
        assert f"https://github.com/{sr.REPO}.git" in clone
        assert all("ghp_secret" not in " ".join(argv) for argv, _ in calls)
        helper = next(argv for argv, _ in calls if "credential.helper" in argv)
        assert "$GITHUB_TOKEN" in helper[-1]
        assert all(cwd == tmp_path / "repo" for _, cwd in calls[1:])


class TestIncidentText:
    def test_describe_lists_the_key_and_samples(self):
        i = incident("fp1", count=3)
        text = sr._describe(i.key, i.evidence)
        assert "workload: hub" in text and "occurrences: 3 in 1 pod(s)" in text
        assert text.endswith("  hub-1/c: Error fp1")

    def test_context_without_a_sample_pointer_is_none(self, monkeypatch):
        monkeypatch.setattr(sr, "query_context", pytest.fail)
        assert sr._context(incident("fp1", first_pod="").evidence, "c") == "(none)"

    @pytest.mark.parametrize("lines,expected", [(["a", "b"], "a\nb"), ([], "(none)")])
    def test_context_joins_the_surrounding_lines(self, monkeypatch, lines, expected):
        asked = []

        def query_context(base, namespace, pod, container, ts):
            asked.append((base, namespace, pod, container, ts))
            return lines

        monkeypatch.setattr(sr, "query_context", query_context)
        i = incident("fp1")
        assert sr._context(i.evidence, "notebook") == expected
        assert asked == [(sr.LOKI_URL, "cms", "hub-1", "notebook", i.evidence.first_ts)]


class TestWatch:
    def test_reads_infrastructure_then_user_workloads(self, monkeypatch):
        queried = []

        def query_loki(base, namespace, start, end, prefixes):
            queried.append(prefixes)
            pod = "purdue-af-1" if prefixes == sr.USER_WORKLOADS else "hub-1-abcde"
            return [
                {
                    "pod": pod,
                    "container": "c",
                    "ts": "2026-09-16T10:00:00+00:00",
                    "line": "Error boom",
                }
            ]

        monkeypatch.setattr(sr, "query_loki", query_loki)
        start = datetime(2026, 9, 16, 9, tzinfo=timezone.utc)
        incidents = sr.watch(start, datetime(2026, 9, 16, 10, tzinfo=timezone.utc))

        infrastructure, user = queried
        assert not set(infrastructure) & set(sr.IGNORED_WORKLOADS)
        assert "hub" in infrastructure
        assert user == sr.USER_WORKLOADS
        assert [i.key.workload for i in incidents] == ["hub", "purdue-af"]


class TestGenAI:
    def test_chat_is_authenticated_and_parsed(self, monkeypatch):
        seen = {}

        def urlopen(request, timeout):
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data)
            return Response(b'{"choices": []}')

        monkeypatch.setenv("GENAI_API_KEY", "k")
        monkeypatch.setattr(sr.urllib.request, "urlopen", urlopen)

        assert sr._genai_chat({"model": "m"}, timeout=1) == {"choices": []}
        assert seen == {"auth": "Bearer k", "body": {"model": "m"}}

    @pytest.mark.parametrize(
        "outcome,error",
        [
            (Response(b"null"), sr.RateLimited),
            (http_error(400, b'{"detail":"Rate limit exceeded"}'), sr.RateLimited),
            (http_error(404, b"model not found"), RuntimeError),
        ],
    )
    def test_chat_errors(self, monkeypatch, outcome, error):
        def urlopen(request, timeout):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setenv("GENAI_API_KEY", "k")
        monkeypatch.setattr(sr.urllib.request, "urlopen", urlopen)

        with pytest.raises(error):
            sr._genai_chat({}, timeout=1)

    def answers(self, monkeypatch, *outcomes):
        queue = list(outcomes)
        bodies = []

        def chat(body, timeout):
            bodies.append(body)
            outcome = queue.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(sr, "_genai_chat", chat)
        return bodies

    def test_probe_trusts_models_of_other_providers(self, monkeypatch):
        self.answers(monkeypatch)
        assert sr._probe("opencode/big-pickle") == (True, 0.0, "not probed")

    def test_probe_reports_the_answer(self, monkeypatch):
        bodies = self.answers(
            monkeypatch, {"choices": [{"message": {"content": " OK "}}]}
        )
        available, _, detail = sr._probe("genai/gemma4:26b-a4b")
        assert available and detail == "answered 'OK'"
        assert bodies[0]["model"] == "gemma4:26b-a4b"

    def test_a_rate_limited_probe_is_answering(self, monkeypatch, no_sleep):
        limited = sr.RateLimited("GenAI Studio answered null (rate limit)")
        self.answers(monkeypatch, limited, limited, limited)
        available, _, detail = sr._probe("genai/m")
        assert available and "rate limit" in detail
        assert no_sleep == [10, 10]

    def test_a_rate_limit_then_an_answer(self, monkeypatch, no_sleep):
        self.answers(
            monkeypatch,
            sr.RateLimited("x"),
            {"choices": [{"message": {"content": None}}]},
        )
        assert sr._probe("genai/m")[2] == "answered ''"

    @pytest.mark.parametrize(
        "outcome",
        [
            TimeoutError("timed out"),
            RuntimeError("GenAI Studio HTTP 502"),
            {"detail": "model not found"},
            {"choices": []},
            ["not", "a", "dict"],
        ],
    )
    def test_a_failed_or_malformed_probe_is_no_answer(self, monkeypatch, outcome):
        self.answers(monkeypatch, outcome)
        available, _, detail = sr._probe("genai/m")
        assert available is False and detail

    def test_ask_returns_the_content_as_json_mode(self, monkeypatch):
        bodies = self.answers(
            monkeypatch, {"choices": [{"message": {"content": '{"groups": []}'}}]}
        )
        assert sr._ask_genai("group these", "genai/gpt-oss:120b") == '{"groups": []}'
        assert bodies[0]["model"] == "gpt-oss:120b"
        assert bodies[0]["response_format"] == {"type": "json_object"}

    def test_ask_retries_one_rate_limit(self, monkeypatch, no_sleep):
        self.answers(
            monkeypatch,
            sr.RateLimited("x"),
            {"choices": [{"message": {"content": "{}"}}]},
        )
        assert sr._ask_genai("p", "genai/m") == "{}"
        assert no_sleep == [15]

    @pytest.mark.parametrize(
        "outcomes,message",
        [
            ((sr.RateLimited("x"), sr.RateLimited("x")), "rate limited twice"),
            ((ConnectionRefusedError("refused"),), "unreachable"),
        ],
    )
    def test_ask_failures_are_runtime_errors(
        self, monkeypatch, no_sleep, outcomes, message
    ):
        self.answers(monkeypatch, *outcomes)
        with pytest.raises(RuntimeError, match=message):
            sr._ask_genai("p", "genai/m")


class TestPushMetrics:
    def metrics(self):
        return sr.TickMetrics(
            started=1.0, outcome="ok", model="genai/m", models=["genai/m"]
        )

    def gateway(self, monkeypatch, page, put_error=None):
        requests = []

        def urlopen(request, timeout):
            if isinstance(request, str):
                if isinstance(page, BaseException):
                    raise page
                return Response(page)
            requests.append(request)
            if put_error:
                raise put_error
            return Response(b"")

        monkeypatch.setattr(sr.urllib.request, "urlopen", urlopen)
        return requests

    def test_counters_continue_from_the_gateway(self, monkeypatch, capsys):
        page = b'self_repair_ticks_total{job="self-repair",outcome="ok"} 4\n'
        requests = self.gateway(monkeypatch, page)

        sr._push_metrics(self.metrics())

        (put,) = requests
        assert put.get_method() == "PUT"
        assert put.full_url.endswith("/metrics/job/self-repair")
        assert 'self_repair_ticks_total{outcome="ok"} 5' in put.data.decode()
        assert "pushed tick outcome=ok" in capsys.readouterr().out

    def test_unreadable_gateway_restarts_the_counters(self, monkeypatch, capsys):
        requests = self.gateway(monkeypatch, ConnectionRefusedError("refused"))

        sr._push_metrics(self.metrics())

        assert 'self_repair_ticks_total{outcome="ok"} 1' in requests[0].data.decode()
        assert "counters restart" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "page,put_error",
        [
            (b"", ConnectionRefusedError("refused")),
            (b"\xff\xfe not utf-8", None),
        ],
    )
    def test_a_failed_push_is_never_fatal(self, monkeypatch, capsys, page, put_error):
        self.gateway(monkeypatch, page, put_error)

        sr._push_metrics(self.metrics())

        assert "push failed" in capsys.readouterr().out


class TestDedupe:
    def test_fewer_than_two_incidents_need_no_model(self, monkeypatch):
        monkeypatch.setattr(sr, "_ask_genai", pytest.fail)
        groups = sr.dedupe([incident("a")], "genai/m")
        assert [g.members for g in groups] == [["a"]]

    def test_the_model_merges_incidents(self, monkeypatch, capsys):
        reply = '{"groups": [{"label": "one cause", "members": ["a", "b"]}]}'
        monkeypatch.setattr(sr, "_ask_genai", lambda prompt, model: reply)
        groups = sr.dedupe([incident("a", 2), incident("b"), incident("c")], "m")
        assert [(g.label, g.members) for g in groups] == [
            ("one cause", ["a", "b"]),
            ("Error c", ["c"]),
        ]
        assert "one cause: a, b" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "error", [RuntimeError("rate limited twice"), json.JSONDecodeError("x", "", 0)]
    )
    def test_a_failed_call_means_singletons(self, monkeypatch, error):
        def ask(prompt, model):
            raise error

        monkeypatch.setattr(sr, "_ask_genai", ask)
        groups = sr.dedupe([incident("a"), incident("b")], "m")
        assert sorted(g.members[0] for g in groups) == ["a", "b"]


class TestAnalyze:
    def test_runs_read_only_on_a_fresh_checkout(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(sr, "_clone", lambda dest: seen.setdefault("repo", dest))
        monkeypatch.setattr(sr, "_context", lambda evidence, container: "ctx line")

        def run_agent(cwd, prompt, permission, label, model):
            seen.update(cwd=cwd, prompt=prompt, permission=permission, model=model)
            return 'thinking... {"fixable": true, "confidence": 0.8, "title": "T"}'

        monkeypatch.setattr(sr, "_run_agent", run_agent)
        i = incident("fp1")

        verdict = sr.analyze(i.key, i.evidence, "genai/m")

        assert verdict.fixable and verdict.confidence == 0.8 and verdict.title == "T"
        assert seen["cwd"] == seen["repo"]
        assert seen["permission"] is sr.READ_ONLY
        assert "ctx line" in seen["prompt"] and "Error fp1" in seen["prompt"]
        assert f"about {sr.AGENT_BUDGET_MINUTES} minutes" in seen["prompt"]

    def test_cache_ignores_evidence_and_model(self):
        cache = sr.analyze.task_options["cache"]
        assert set(cache["ignored_inputs"]) == {"evidence", "model"}


class TestFix:
    VERDICT = sr.Verdict(True, 0.9, "apps/x", "Fix x", "Because.", "Edit x.")

    @pytest.fixture
    def env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        state = SimpleNamespace(
            open_pr="",
            status=" M apps/x/values.yaml\n",
            diff="-a: 1\n+a: 2\n",
            defects="",
            git=[],
            created=[],
            prompt=None,
        )

        def git(*args, cwd):
            state.git.append(args)
            return {"status": state.status, "diff": state.diff}.get(args[0], "")

        def run_agent(cwd, prompt, permission, label, model):
            state.prompt, state.permission = prompt, permission
            return "I changed x."

        def create(repo, token, branch, base, title, body):
            state.created.append((repo, token, branch, base, title, body))
            return "https://github.com/PurdueAF/purdue-af/pull/9"

        monkeypatch.setattr(sr, "open_pull_request", lambda *a: state.open_pr)
        monkeypatch.setattr(sr, "_clone", lambda dest: None)
        monkeypatch.setattr(sr, "_git", git)
        monkeypatch.setattr(sr, "_context", lambda evidence, container: "ctx")
        monkeypatch.setattr(sr, "_run_agent", run_agent)
        monkeypatch.setattr(sr, "_python_defects", lambda repo, paths: state.defects)
        monkeypatch.setattr(sr, "create_pull_request", create)
        monkeypatch.setattr(
            sr.flyte,
            "ctx",
            lambda: SimpleNamespace(action=SimpleNamespace(run_name="run-1")),
        )
        return state

    def fix(self):
        i = incident("abcdef123456")
        return sr.fix(i.key, i.evidence, self.VERDICT, "genai/m")

    def test_an_open_pull_request_is_reused(self, env):
        env.open_pr = "https://github.com/PurdueAF/purdue-af/pull/1"
        assert self.fix() == env.open_pr
        assert env.git == [] and env.created == []

    def test_a_clean_change_becomes_a_draft_pull_request(self, env):
        assert self.fix().endswith("/pull/9")
        commands = [args[0] for args in env.git]
        assert commands == ["checkout", "status", "diff", "add", "commit", "push"]
        assert env.git[0] == ("checkout", "-q", "-b", "self-repair-abcdef123456")
        assert env.git[-1] == (
            "push",
            "-q",
            "--force",
            "origin",
            "self-repair-abcdef123456",
        )
        assert "self-repair fingerprint abcdef123456" in env.git[4][-1]
        assert env.permission is sr.EDIT
        assert "Edit x." in env.prompt
        repo, token, branch, base, title, body = env.created[0]
        assert (repo, token, branch, base, title) == (
            sr.REPO,
            "tok",
            "self-repair-abcdef123456",
            "main",
            "Fix x",
        )
        assert "run-1" in body and "I changed x." in body and "genai/m" in body

    def test_the_run_name_is_optional(self, env, monkeypatch):
        monkeypatch.setattr(sr.flyte, "ctx", lambda: None)
        assert self.fix()
        assert "Flyte run ``" in env.created[0][-1]

    @pytest.mark.parametrize(
        "change",
        [
            {"status": ""},
            {"status": "R  apps/a.py -> pixi/global/pixi.toml\n"},
            {"diff": '-log.error("x")\n+log.warning("x")\n'},
            {"defects": "bad.py:1:1: F821 undefined name"},
        ],
    )
    def test_changes_that_are_not_fixes_open_nothing(self, env, change):
        for name, value in change.items():
            setattr(env, name, value)
        assert self.fix() == ""
        assert env.created == []
        assert "push" not in [args[0] for args in env.git]


class FakeProc:
    """`opencode run` whose stdout is the given lines."""

    def __init__(self, lines, stderr="", hang=False):
        self.lines, self.stderr_text, self.hang = lines, stderr, hang
        self.killed = threading.Event()
        self.argv = None

    def __call__(self, argv, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        kwargs["stderr"].write(self.stderr_text)
        kwargs["stderr"].flush()
        self.stdout = self._stdout()
        return self

    def _stdout(self):
        yield from self.lines
        if self.hang:
            self.killed.wait(5)

    def poll(self):
        return 0 if self.killed.is_set() else None

    def kill(self):
        self.killed.set()

    def wait(self):
        return 0


def event(kind, **part):
    return json.dumps({"type": kind, "part": part}) + "\n"


class TestAgentSession:
    @pytest.fixture(autouse=True)
    def quiet_log(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sr, "OPENCODE_LOG", tmp_path / "absent.log")

    def session(self, monkeypatch, proc, tmp_path):
        monkeypatch.setattr(sr.subprocess, "Popen", proc)
        return sr._agent_session(tmp_path, "prompt", "/cfg.json", "fp", "genai/m")

    def test_the_final_turn_is_the_answer(self, monkeypatch, tmp_path, capsys):
        proc = FakeProc(
            [
                "not json\n",
                event("text", text="done"),
                event("step_finish", reason="stop"),
            ],
            stderr="deprecation notice",
        )
        assert self.session(monkeypatch, proc, tmp_path) == "done"
        assert proc.argv[:2] == ["opencode", "run"]
        assert proc.argv[proc.argv.index("--model") + 1] == "genai/m"
        assert proc.kwargs["env"]["OPENCODE_CONFIG"] == "/cfg.json"
        assert proc.killed.is_set(), "a lingering opencode is killed"
        out = capsys.readouterr().out
        assert "fp: agent says: done" in out
        assert "opencode stderr: deprecation notice" in out

    def test_a_rate_limit_event_is_a_provider_error(self, monkeypatch, tmp_path):
        proc = FakeProc(
            [json.dumps({"type": "error", "error": {"message": "Rate limit"}})]
        )
        with pytest.raises(sr.ProviderError, match="rate limit"):
            self.session(monkeypatch, proc, tmp_path)

    def test_other_errors_fail_the_session(self, monkeypatch, tmp_path):
        proc = FakeProc([json.dumps({"type": "error", "error": "model not found"})])
        with pytest.raises(RuntimeError, match="model not found") as excinfo:
            self.session(monkeypatch, proc, tmp_path)
        assert not isinstance(excinfo.value, sr.ProviderError)

    def test_no_final_answer_in_time_is_killed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sr, "AGENT_TIMEOUT_S", 0.05)
        proc = FakeProc([event("text", text="thinking")], hang=True)
        with pytest.raises(RuntimeError, match="no final answer within"):
            self.session(monkeypatch, proc, tmp_path)

    def test_a_watched_provider_error_ends_the_session(self, monkeypatch, tmp_path):
        class Aborted(sr._Watch):
            def start(self):
                super().start()
                self.aborted = "AI_APICallError: stream error"

        monkeypatch.setattr(sr, "_Watch", Aborted)
        with pytest.raises(sr.ProviderError, match="stream error"):
            self.session(monkeypatch, FakeProc([]), tmp_path)


class TestWatchdog:
    def watch(self, monkeypatch, tmp_path, appended, **settings):
        log = tmp_path / "opencode.log"
        log.write_text("level=ERROR message=old-line-before-start\n")
        monkeypatch.setattr(sr, "OPENCODE_LOG", log)
        for name, value in settings.items():
            monkeypatch.setattr(sr, name, value)
        proc = FakeProc([])
        watch = sr._Watch("fp", proc)
        ticks = iter([False, True])

        def wait(_timeout):
            with log.open("a") as handle:
                handle.write(appended)
            return next(ticks)

        watch._stop = SimpleNamespace(wait=wait)
        return watch, proc

    def test_provider_error_then_silence_kills_the_session(
        self, monkeypatch, tmp_path, capsys
    ):
        rate_limit = (
            'level=ERROR message="stream error" '
            'error.error="AI_APICallError: Rate limit exceeded"\n'
        )
        watch, proc = self.watch(
            monkeypatch, tmp_path, "level=INFO x=1\n" + rate_limit, PROVIDER_GRACE_S=0
        )
        watch.saw("earlier")
        watch.last_event_at -= 1

        watch._run()

        assert watch.aborted == "AI_APICallError: Rate limit exceeded"
        assert proc.killed.is_set()
        out = capsys.readouterr().out
        assert "old-line-before-start" not in out
        assert "fp: opencode error: message=stream error" in out

    def test_silence_is_reported_by_heartbeat(self, monkeypatch, tmp_path, capsys):
        watch, proc = self.watch(
            monkeypatch,
            tmp_path,
            'level=WARN message="slow"\n',
            HEARTBEAT_S=0,
        )

        watch._run()

        assert watch.aborted is None and not proc.killed.is_set()
        assert "fp: agent silent for" in capsys.readouterr().out

    def test_start_and_stop_run_the_thread(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sr, "OPENCODE_LOG", tmp_path / "absent.log")
        watch = sr._Watch("fp", FakeProc([]))
        watch.start()
        watch.stop()
        assert not watch._thread.is_alive()


class FakeProxy:
    url = "http://127.0.0.1:1"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestRunAgent:
    @pytest.fixture
    def sessions(self, monkeypatch, tmp_path):
        state = SimpleNamespace(outcomes=["answer"], configs=[])

        def session(cwd, prompt, config_path, label, model):
            with open(config_path) as handle:
                state.configs.append(json.load(handle))
            outcome = state.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(sr, "Proxy", FakeProxy)
        monkeypatch.setattr(sr, "_agent_session", session)
        monkeypatch.setattr(sr, "PLATFORM_CONTEXT", tmp_path / "absent.md")
        monkeypatch.setattr(sr.random, "uniform", lambda a, b: a)
        return state

    def test_config_points_opencode_at_the_proxy(self, sessions, tmp_path):
        assert sr._run_agent(tmp_path, "p", sr.READ_ONLY, "fp", "genai/m") == "answer"
        (config,) = sessions.configs
        assert config["model"] == "genai/m"
        assert config["permission"] == sr.READ_ONLY
        assert config["share"] == "disabled"
        options = config["provider"]["genai"]["options"]
        assert options["baseURL"] == "http://127.0.0.1:1/api"
        assert "baseURL" not in sr.PROVIDERS["genai"]["options"], "not mutated"
        assert "instructions" not in config

    def test_platform_context_is_handed_over_when_mounted(
        self, sessions, monkeypatch, tmp_path
    ):
        context = tmp_path / "platform-context.md"
        context.write_text("facts")
        monkeypatch.setattr(sr, "PLATFORM_CONTEXT", context)
        sr._run_agent(tmp_path, "p", sr.EDIT, "fp", "genai/m")
        assert sessions.configs[0]["instructions"] == [str(context)]

    def test_rate_limits_are_retried_with_a_pause(self, sessions, no_sleep, tmp_path):
        limited = sr.ProviderError("rate limit after 3s")
        sessions.outcomes = [limited, limited, "answer"]
        assert sr._run_agent(tmp_path, "p", sr.EDIT, "fp", "genai/m") == "answer"
        assert no_sleep == [60, 60]

    def test_rate_limits_give_up_after_the_retries(self, sessions, no_sleep, tmp_path):
        limited = sr.ProviderError("rate limit after 3s")
        sessions.outcomes = [limited] * (sr.RATE_LIMIT_RETRIES + 1)
        with pytest.raises(sr.ProviderError):
            sr._run_agent(tmp_path, "p", sr.EDIT, "fp", "genai/m")
        assert len(no_sleep) == sr.RATE_LIMIT_RETRIES

    def test_other_provider_errors_are_not_retried(self, sessions, no_sleep, tmp_path):
        sessions.outcomes = [sr.ProviderError("provider error after 9s: boom")]
        with pytest.raises(sr.ProviderError, match="boom"):
            sr._run_agent(tmp_path, "p", sr.EDIT, "fp", "genai/m")
        assert no_sleep == []


class FakeRun:
    def __init__(self, phase, cache, output):
        self.url = "https://flyte/run"
        status = SimpleNamespace(phase=phase, cache_status=cache)
        details = SimpleNamespace(
            action_details=SimpleNamespace(pb2=SimpleNamespace(status=status))
        )
        self.wait = SimpleNamespace(aio=self._async(None))
        self.details = SimpleNamespace(aio=self._async(details))
        self.typed_outputs = SimpleNamespace(aio=self._async({"o0": output}))

    @staticmethod
    def _async(value):
        async def call(*args, **kwargs):
            return value

        return call


class TestSpawn:
    @pytest.fixture
    def flyteidl(self, monkeypatch):
        enum = SimpleNamespace(Name=lambda value: value)
        common = types.ModuleType("flyteidl2.common")
        common.phase_pb2 = SimpleNamespace(ActionPhase=enum)
        core = types.ModuleType("flyteidl2.core")
        core.catalog_pb2 = SimpleNamespace(CatalogCacheStatus=enum)
        monkeypatch.setitem(sys.modules, "flyteidl2", types.ModuleType("flyteidl2"))
        monkeypatch.setitem(sys.modules, "flyteidl2.common", common)
        monkeypatch.setitem(sys.modules, "flyteidl2.core", core)

    def launcher(self, monkeypatch, run):
        launched = []

        async def aio(task, *args):
            launched.append((task, args))
            return run

        def with_runcontext(name):
            launched.append(name)
            return SimpleNamespace(run=SimpleNamespace(aio=aio))

        monkeypatch.setattr(sr.flyte, "with_runcontext", with_runcontext)
        return launched

    def test_returns_the_output_and_cache_status(self, flyteidl, monkeypatch):
        run = FakeRun("ACTION_PHASE_SUCCEEDED", "CACHE_HIT", ["i"])
        launched = self.launcher(monkeypatch, run)

        result = asyncio.run(sr._spawn("name", sr.watch, 1, 2, output_type=list))

        assert result == (["i"], "CACHE_HIT")
        assert launched == ["name", (sr.watch, (1, 2))]

    def test_an_unsuccessful_run_raises(self, flyteidl, monkeypatch):
        self.launcher(monkeypatch, FakeRun("ACTION_PHASE_FAILED", "CACHE_MISS", None))

        with pytest.raises(RuntimeError, match="name ended in FAILED"):
            asyncio.run(sr._spawn("name", sr.watch, output_type=list))


@pytest.fixture
def reports(monkeypatch):
    pages = []

    async def replace(html, do_flush):
        pages.append(html)

    monkeypatch.setattr(sr.flyte.report.replace, "aio", replace)
    return pages


@pytest.fixture
def pushed(monkeypatch):
    metrics = []
    monkeypatch.setattr(sr, "_push_metrics", metrics.append)
    return metrics


class TestTriage:
    TRIGGER = datetime(2026, 9, 16, 10, 0)

    def test_no_answering_model_ends_the_tick(
        self, monkeypatch, reports, pushed, capsys
    ):
        monkeypatch.setattr(sr, "MODELS", ("genai/a", "genai/b"))
        monkeypatch.setattr(sr, "_probe", lambda model: (False, 30.0, "timeout"))
        monkeypatch.setattr(sr, "_tick", pytest.fail)

        summary = asyncio.run(sr.triage(self.TRIGGER))

        assert summary.outcome == "no_model" and summary.incidents == 0
        assert summary.window_end == "2026-09-16T10:00:00+00:00", "naive means UTC"
        assert summary.window_start == "2026-09-16T08:45:00+00:00"
        assert "No model answered (genai/a, genai/b)" in reports[0]
        (metrics,) = pushed
        assert metrics.outcome == "no_model" and len(metrics.probes) == 2
        assert (
            "model genai/a: no answer after 30.0s (timeout)" in capsys.readouterr().out
        )

    def test_the_first_answering_model_runs_the_tick(
        self, monkeypatch, reports, pushed
    ):
        monkeypatch.setattr(sr, "MODELS", ("genai/a", "genai/b"))
        monkeypatch.setattr(
            sr, "_probe", lambda model: (model == "genai/b", 0.1, "answered")
        )
        seen = {}

        async def tick(tick, start, trigger, model, max_incidents, max_fixes, m):
            seen.update(model=model, limits=(max_incidents, max_fixes), metrics=m)
            return "summary"

        monkeypatch.setattr(sr, "_tick", tick)

        assert asyncio.run(sr.triage(self.TRIGGER, 30, 3, 1)) == "summary"
        assert seen["model"] == "genai/b" and seen["limits"] == (3, 1)
        assert pushed == [seen["metrics"]] and pushed[0].outcome == "ok"
        assert pushed[0].model == "genai/b"

    def test_a_failed_tick_is_still_counted(self, monkeypatch, reports, pushed):
        monkeypatch.setattr(sr, "MODELS", ("genai/a",))
        monkeypatch.setattr(sr, "_probe", lambda model: (True, 0.1, "answered"))

        async def tick(*args):
            raise RuntimeError("watch ended in FAILED")

        monkeypatch.setattr(sr, "_tick", tick)

        with pytest.raises(RuntimeError, match="watch ended"):
            asyncio.run(sr.triage(self.TRIGGER.replace(tzinfo=timezone.utc)))
        assert pushed[0].outcome == "failed"


class TestTick:
    """watch -> dedupe -> analyze within budget -> fix within budget."""

    def spawner(self, monkeypatch, incidents, groups, verdicts, pr_urls):
        spawned = []

        async def spawn(name, task, *args, output_type):
            spawned.append(name)
            if task is sr.watch:
                return incidents, "CACHE_DISABLED"
            if task is sr.dedupe:
                return groups, "CACHE_DISABLED"
            if task is sr.analyze:
                outcome = verdicts[args[0].fingerprint]
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            if task is sr.fix:
                return pr_urls.pop(0), "CACHE_DISABLED"
            raise AssertionError(task)

        monkeypatch.setattr(sr, "_spawn", spawn)
        return spawned

    def tick(self, max_incidents=6, max_fixes=2):
        metrics = sr.TickMetrics(started=0.0, outcome="ok", model="m", models=["m"])
        start = datetime(2026, 9, 16, 9, tzinfo=timezone.utc)
        end = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
        summary = asyncio.run(
            sr._tick("tick1", start, end, "m", max_incidents, max_fixes, metrics)
        )
        return summary, metrics

    def test_a_full_tick(self, monkeypatch, reports):
        incidents = [
            incident("aaaa01", 5),
            incident("aaaa02", 3),
            incident("bbbb01", 4),
            incident("cccc01", 2),
            incident("dddd01", 1),
            incident("eeee01", 1),
        ]
        groups = [
            sr.Group("cause a", ["aaaa01", "aaaa02"]),
            sr.Group("b", ["bbbb01"]),
            sr.Group("c", ["cccc01"]),
            sr.Group("d", ["dddd01"]),
            sr.Group("e", ["eeee01"]),
        ]
        fixable = sr.Verdict(True, 0.9, "apps/x", "Fix", "r")
        verdicts = {
            "aaaa01": (fixable, "CACHE_POPULATED"),
            "bbbb01": (sr.Verdict(True, 0.5, reason="unsure"), "CACHE_HIT"),
            "cccc01": RuntimeError("opencode gave no final answer"),
            "dddd01": (fixable, "CACHE_HIT"),
            "eeee01": (fixable, "CACHE_POPULATED"),
        }
        spawned = self.spawner(
            monkeypatch, incidents, groups, verdicts, ["", "https://gh/pull/1"]
        )

        summary, metrics = self.tick(max_fixes=1)

        assert spawned[:2] == ["self-repair-watch-tick1", "self-repair-dedupe-tick1"]
        assert "self-repair-analyze-tick1-aaaa" in spawned
        assert spawned[-2:] == [
            "self-repair-fix-tick1-aaaa",
            "self-repair-fix-tick1-dddd",
        ], "a failed fix does not use the budget; the third is over it"
        assert summary.incidents == 5 and summary.lines == 16
        assert summary.fixable == 3
        assert summary.pull_requests == ["https://gh/pull/1"]
        assert summary.model == "m"
        assert (metrics.error_lines, metrics.incidents, metrics.groups) == (16, 6, 5)
        assert metrics.analyses == {"fresh": 2, "cache_hit": 2, "failed": 1}
        assert metrics.fixable == 3 and metrics.pull_requests == 1
        assert metrics.fix_failures == 1
        assert len(reports) == 3
        assert "pending" in reports[0]
        final = reports[-1]
        assert "3 of 4 analyzed incidents fixable" in final
        assert "(2 incidents)" in final and 'href="https://gh/pull/1"' in final
        assert "opencode gave no final answer" in final

    def test_incidents_over_the_analysis_budget_are_not_analyzed(
        self, monkeypatch, reports
    ):
        incidents = [incident("aaaa01"), incident("bbbb01")]
        groups = [sr.Group("a", ["aaaa01"]), sr.Group("b", ["bbbb01"])]
        verdicts = {"aaaa01": (sr.Verdict(False, 0.9), "CACHE_POPULATED")}
        self.spawner(monkeypatch, incidents, groups, verdicts, [])

        summary, metrics = self.tick(max_incidents=1)

        assert summary.fixable == 0 and summary.pull_requests == []
        assert metrics.analyses == {"fresh": 1, "cache_hit": 0, "failed": 0}
        assert "not analyzed" in reports[-1]
