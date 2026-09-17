"""Flyte 2 tasks: watch Loki, analyze each error with opencode, and open a
draft pull request when the fix belongs in this repository.

`triage` orchestrates. It starts every other task as a run of its own, named
after the task, so pods read `self-repair-<task>-<tick>[-<fp>]-a0-0`
and each run shows up by name in the console."""

import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import flyte
import prompts
from genai_proxy import Proxy
from triage import (
    ALL_WORKLOADS,
    CACHE_HIT,
    IGNORED_WORKLOADS,
    USER_WORKLOADS,
    WATCHED_WORKLOADS,
    Evidence,
    Group,
    Incident,
    IncidentKey,
    Row,
    Summary,
    TickMetrics,
    Verdict,
    analyze_within_budget,
    cluster,
    collect_reply,
    create_pull_request,
    describe_event,
    exposition,
    grouping_prompt,
    is_rate_limit,
    open_pull_request,
    opencode_log_line,
    parse_counters,
    parse_groups,
    parse_verdict,
    pick_model,
    probe_body,
    pull_request_body,
    query_context,
    query_loki,
    removes_error_handling,
    report_html,
    representative,
    silences,
)

IMAGE = "geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/self-repair:latest"
LOKI_URL = "http://loki.cms.svc.cluster.local:3100"
NAMESPACE = "cms"
REPO = "PurdueAF/purdue-af"
BASE_BRANCH = "main"
# A dash, not a slash: `self-repair/<x>` cannot be created while any branch
# named `self-repair` exists.
BRANCH_PREFIX = "self-repair-"
# Purdue GenAI Studio (docs.rcac.purdue.edu/services/genai): OpenAI-compatible,
# on Purdue's network, keyed to the account whose key is in GENAI_API_KEY
# (podtemplate.yaml). Documented limits: 60 requests/min per user, about 10
# concurrent calls per model, and a rate limit answers with a JSON null body.
# gpt-oss:120b answered a tool-calling probe in 0.4 s; qwen3.6:27b timed out.
# Through the proxy, gpt-oss:120b and gemma4:26b-a4b each ran the whole tool
# loop in about 10 s (2026-09-16); qwen3.6:27b was not answering that day, and
# gemma4 answered nothing for most of the following night. Models hang one at
# a time, so every tick starts by probing these in order and runs on the first
# that answers; a tick with no answering model does nothing else. llama4 is on
# a different serving stack from the others. SELF_REPAIR_MODELS (comma-
# separated) overrides the list without a code change.
MODELS = tuple(
    model.strip()
    for model in os.environ.get(
        "SELF_REPAIR_MODELS",
        "genai/gemma4:26b-a4b,genai/gpt-oss:120b,genai/llama4:latest",
    ).split(",")
    if model.strip()
)
PROBE_TIMEOUT_S = 30
PROBE_RATE_LIMIT_RETRIES = 2
# The AF Prometheus pushgateway (apps/monitoring/prometheus): one push per tick.
PUSHGATEWAY_URL = os.environ.get(
    "SELF_REPAIR_PUSHGATEWAY",
    "http://prometheus-prometheus-pushgateway.cms.svc.cluster.local:9091",
)
METRICS_JOB = "self-repair"
# The vLLM-backed models with native tool calling; deployed context per the docs.
PROVIDERS = {
    "genai": {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Purdue GenAI Studio",
        # baseURL is filled in per session: opencode talks to the re-framing
        # proxy in genai_proxy.py, which talks to GenAI Studio.
        "options": {"apiKey": "{env:GENAI_API_KEY}"},
        "models": {
            model_id: {
                "name": name,
                "tool_call": True,
                "reasoning": False,
                "temperature": True,
                "release_date": "2026-06-01",
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": context, "output": 8192},
            }
            for model_id, name, context in (
                ("gpt-oss:120b", "gpt-oss 120b", 65536),
                ("qwen3.6:27b", "Qwen 3.6 27B", 65536),
                ("gemma4:26b-a4b", "Gemma 4 26B", 65536),
                ("llama4:latest", "Llama 4", 16384),
            )
        },
    }
}
MIN_CONFIDENCE = 0.7
# Hard stop for one opencode session. The prompt tells the agent the soft
# budget, a few minutes less, so it decides before the clock does.
AGENT_TIMEOUT_S = 30 * 60
AGENT_BUDGET_MINUTES = 25
# The suite CI's check-unit job runs: a change that fails it fails CI too, so
# the pull request would only cost a reviewer the time to find that out. The
# two bounds have to fit beside AGENT_TIMEOUT_S in this task's 60 minutes;
# the suite itself takes about a minute and the environment a few.
UNIT_TEST_PROJECT = ("--project", "tests", "--frozen")
UNIT_TEST_ARGS = (
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "-c",
    "tests/pyproject.toml",
    "tests",
)
BUILD_TIMEOUT_S = 600
UNIT_TEST_TIMEOUT_S = 600
LOG_INCIDENTS = 20

# external_directory: the agent may look at the parent of its checkout; in a
# non-interactive run a permission prompt is auto-rejected and ends the session.
READ_ONLY = {"edit": "deny", "bash": "deny", "external_directory": "allow"}
# Not fixable here by definition; the agent may not edit them and a change
# touching them never becomes a PR. Patterns are opencode permission globs.
PROTECTED_PATHS = ("docker/dask-gateway-server/", "pixi/", "deploy/")
EDIT = {
    "edit": {
        "*docker/dask-gateway-server/*": "deny",
        "*/pixi/*": "deny",
        "*/deploy/*": "deny",
        "*.lock": "deny",
        "*": "allow",
    },
    "external_directory": "allow",
    "bash": {
        "git push*": "deny",
        "git commit*": "deny",
        "git checkout*": "deny",
        "git reset*": "deny",
        "git clean*": "deny",
        "*": "allow",
    },
}

env = flyte.TaskEnvironment(
    name="self-repair",
    image=IMAGE,
    # apps/self-repair/podtemplate.yaml: node placement and the GitHub token.
    pod_template="self-repair",
    resources=flyte.Resources(cpu=1, memory="2Gi"),
)


def _log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {message}", flush=True)


# Run names are capped at 30 characters (the pod is `<run>-a0-0`), and every
# one starts with the 12 of "self-repair-". A tick is the launch minute in
# base36: 5 characters until late 2084. Four characters of fingerprint tell the
# analyses of one tick apart; the full fingerprint is in the logs and branch.
NAME_LIMIT = 30
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def tick_of(moment: datetime) -> str:
    minutes = int(moment.timestamp()) // 60
    out = ""
    while minutes:
        minutes, digit = divmod(minutes, 36)
        out = DIGITS[digit] + out
    return out.rjust(5, "0")


def run_name(task: str, tick: str, fingerprint: str = "") -> str:
    name = f"self-repair-{task}-{tick}" + (f"-{fingerprint[:4]}" if fingerprint else "")
    if len(name) > NAME_LIMIT:
        raise ValueError(f"run name {name!r} is longer than {NAME_LIMIT}")
    return name


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return proc.stdout


def _clone(dest: Path) -> None:
    started = time.monotonic()
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--branch",
            BASE_BRANCH,
            f"https://github.com/{REPO}.git",
            str(dest),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    # The token reaches git only through this helper, at push time: never in a
    # remote URL, never in .git/config, never in an error message.
    _git(
        "config",
        "credential.helper",
        "!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f",
        cwd=dest,
    )
    _git("config", "user.name", "self-repair", cwd=dest)
    _git("config", "user.email", "self-repair@users.noreply.github.com", cwd=dest)
    head = _git("rev-parse", "--short", "HEAD", cwd=dest).strip()
    _log(f"cloned {REPO}@{head} ({BASE_BRANCH}) in {time.monotonic() - started:.0f}s")


OPENCODE_LOG = Path.home() / ".local/share/opencode/log/opencode.log"
HEARTBEAT_S = 60
# A provider error followed by this much silence means opencode is not
# retrying: stop waiting for the hard timeout.
PROVIDER_GRACE_S = 120
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_PAUSE_S = (60, 120)


class _Watch:
    """Relays opencode's own log into ours, heartbeats through silence, and
    pulls the plug when a provider error is followed by silence."""

    def __init__(self, label: str, proc: subprocess.Popen[str]) -> None:
        self.label = label
        self.proc = proc
        self.last_event = "start"
        self.last_event_at = time.monotonic()
        self.provider_error: str | None = None
        self.provider_error_at = 0.0
        self.aborted: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def saw(self, event: str) -> None:
        self.last_event = event
        self.last_event_at = time.monotonic()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        offset = OPENCODE_LOG.stat().st_size if OPENCODE_LOG.exists() else 0
        next_heartbeat = time.monotonic() + HEARTBEAT_S
        while not self._stop.wait(2):
            if OPENCODE_LOG.exists():
                with OPENCODE_LOG.open(errors="replace") as handle:
                    handle.seek(offset)
                    fresh = handle.read()
                    offset = handle.tell()
                for raw in fresh.splitlines():
                    parsed = opencode_log_line(raw)
                    if parsed is None:
                        continue
                    summary, error = parsed
                    _log(f"{self.label}: opencode {summary}")
                    if error:
                        self.provider_error = error
                        self.provider_error_at = time.monotonic()
            now = time.monotonic()
            silent = now - self.last_event_at
            if (
                self.provider_error
                and self.provider_error_at > self.last_event_at
                and now - self.provider_error_at >= PROVIDER_GRACE_S
            ):
                self.aborted = self.provider_error
                _log(
                    f"{self.label}: no event for {now - self.provider_error_at:.0f}s after a provider "
                    f"error, giving up on this session: {self.provider_error}"
                )
                self.proc.kill()
                return
            if now >= next_heartbeat:
                if silent >= HEARTBEAT_S:
                    _log(
                        f"{self.label}: agent silent for {silent:.0f}s (last: {self.last_event[:120]})"
                    )
                next_heartbeat = now + HEARTBEAT_S


def _agent_session(
    cwd: Path, prompt: str, config_path: str, label: str, model: str
) -> str:
    started = time.monotonic()
    stderr = tempfile.NamedTemporaryFile(
        "w+", suffix=".log", prefix="opencode-", delete=False
    )
    proc = subprocess.Popen(
        [
            "opencode",
            "run",
            "--format",
            "json",
            "--model",
            model,
            "--dir",
            str(cwd),
            prompt,
        ],
        cwd=cwd,
        env={**os.environ, "OPENCODE_CONFIG": config_path},
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
    )
    watch = _Watch(label, proc)

    def narrate(event: dict[str, Any]) -> None:
        line = describe_event(event)
        if line:
            _log(f"{label}: agent {line}")
            watch.saw(line)

    # The answer is complete at the model's final turn; the process is not
    # trusted to exit after it (opencode 1.18 lingers), so a timer bounds the
    # whole thing and the reader stops on its own.
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(AGENT_TIMEOUT_S, expire)
    timer.start()
    watch.start()
    try:
        assert proc.stdout is not None
        reply = collect_reply(proc.stdout, narrate)
    finally:
        timer.cancel()
        watch.stop()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        stderr.seek(0)
        errors = stderr.read().strip()
        stderr.close()
    elapsed = time.monotonic() - started
    if errors:
        _log(f"{label}: opencode stderr: {errors[-1500:]}")
    if reply.finished:
        _log(f"{label}: agent finished in {elapsed:.0f}s")
        return reply.text
    if watch.aborted:
        raise ProviderError(f"provider error after {elapsed:.0f}s: {watch.aborted}")
    if reply.error and is_rate_limit(reply.error):
        raise ProviderError(f"rate limit after {elapsed:.0f}s: {reply.error[:200]}")
    if timed_out.is_set():
        raise RuntimeError(
            f"opencode gave no final answer within {AGENT_TIMEOUT_S}s (killed)"
        )
    raise RuntimeError(f"opencode failed after {elapsed:.0f}s: {reply.error}")


class ProviderError(RuntimeError):
    pass


# The platform context every agent in an AF session reads, mounted by
# apps/self-repair/podtemplate.yaml from the same file the purdue-af image
# carries at this path. Handed to opencode the way config-agents.sh does it:
# `instructions`, the additive channel, not an AGENTS.md.
PLATFORM_CONTEXT = Path("/opt/purdue-af/agents/platform-context.md")


def _run_agent(
    cwd: Path, prompt: str, permission: dict[str, Any], label: str, model: str
) -> str:
    mode = "read-only" if permission is READ_ONLY else "edit"
    # opencode talks to the re-framing proxy (genai_proxy.py), the proxy to
    # GenAI Studio.
    with Proxy() as proxy:
        providers = json.loads(json.dumps(PROVIDERS))
        providers["genai"]["options"]["baseURL"] = f"{proxy.url}/api"
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": model,
            "provider": providers,
            "permission": permission,
            "share": "disabled",
        }
        if PLATFORM_CONTEXT.is_file():
            config["instructions"] = [str(PLATFORM_CONTEXT)]
        else:
            _log(
                f"{label}: no platform context at {PLATFORM_CONTEXT}; agent runs without it"
            )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="opencode-", delete=False
        ) as handle:
            json.dump(config, handle)
        for attempt in range(1, RATE_LIMIT_RETRIES + 2):
            _log(f"{label}: opencode {model} ({mode}) in {cwd}, attempt {attempt}")
            try:
                return _agent_session(cwd, prompt, handle.name, label, model)
            except ProviderError as exc:
                if "rate limit" not in str(exc).lower() or attempt > RATE_LIMIT_RETRIES:
                    raise
                pause = random.uniform(*RATE_LIMIT_PAUSE_S)
                _log(f"{label}: rate limited; retrying in {pause:.0f}s")
                time.sleep(pause)
    raise AssertionError("unreachable")


def _context(evidence: Evidence, container: str) -> str:
    if not evidence.first_pod:
        _log("no sample pointer on the evidence; analyzing the samples alone")
        return "(none)"
    lines = query_context(
        LOKI_URL, NAMESPACE, evidence.first_pod, container, evidence.first_ts
    )
    _log(
        f"{len(lines)} surrounding line(s) from {evidence.first_pod}/{container} around {evidence.first_ts[11:19]}"
    )
    return "\n".join(lines) if lines else "(none)"


def _describe(key: IncidentKey, evidence: Evidence) -> str:
    return "\n".join(
        [
            f"workload: {key.workload}",
            f"container: {key.container}",
            f"occurrences: {evidence.count} in {evidence.pods} pod(s) between {evidence.first_seen} and {evidence.last_seen}",
            f"normalized message: {key.message}",
            "sample lines:",
            *(f"  {sample}" for sample in evidence.samples),
        ]
    )


@env.task(retries=2, timeout=timedelta(minutes=5))
def watch(start: datetime, end: datetime) -> list[Incident]:
    _log(
        f"querying {LOKI_URL} for error lines in {NAMESPACE}, {start:%H:%M:%S}..{end:%H:%M:%S} UTC, "
        f"from {len(ALL_WORKLOADS)} watched workloads (triage.WATCHED_WORKLOADS + USER_WORKLOADS - IGNORED_WORKLOADS)"
    )
    infrastructure = tuple(p for p in WATCHED_WORKLOADS if p not in IGNORED_WORKLOADS)
    lines = query_loki(LOKI_URL, NAMESPACE, start, end, prefixes=infrastructure)
    user_lines = query_loki(LOKI_URL, NAMESPACE, start, end, prefixes=USER_WORKLOADS)
    _log(
        f"{len(lines)} infrastructure lines, {len(user_lines)} user-workload lines "
        "(each query capped at 5000)"
    )
    lines += user_lines
    incidents = cluster(lines)
    _log(
        f"{len(lines)} error lines in {len(incidents)} distinct incident(s); top {min(LOG_INCIDENTS, len(incidents))}:"
    )
    for incident in incidents[:LOG_INCIDENTS]:
        key, evidence = incident.key, incident.evidence
        _log(
            f"  {key.fingerprint} x{evidence.count} in {evidence.pods} pod(s) "
            f"{key.workload}/{key.container}: {key.message[:100]}"
        )
    return incidents


GENAI_CHAT = "https://genai.rcac.purdue.edu/api/chat/completions"


class RateLimited(RuntimeError):
    pass


def _genai_chat(body: dict[str, Any], timeout: float) -> Any:
    """One non-streaming chat completion at GenAI Studio, parsed. RateLimited
    for its two rate-limit shapes (HTTP 400 with the text, or a null body);
    RuntimeError for any other HTTP error; OSError for timeouts and the like."""
    request = urllib.request.Request(
        GENAI_CHAT,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['GENAI_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        if "Rate limit" in detail:
            raise RateLimited(f"GenAI Studio rate limited: {detail[:120]}") from exc
        raise RuntimeError(f"GenAI Studio HTTP {exc.code}: {detail[:300]}") from exc
    if data is None:
        raise RateLimited("GenAI Studio answered null (rate limit)")
    return data


def _probe(model: str) -> tuple[bool, float, str]:
    """Does the model answer at all? A tiny completion with a short deadline,
    so a hung backend costs seconds, not the five minutes its gateway takes
    to give up. A rate limit means it is answering. Only genai/* models are
    probed; anything else is taken on trust."""
    provider, _, model_id = model.partition("/")
    if provider != "genai":
        return True, 0.0, "not probed"
    started = time.monotonic()
    for attempt in range(PROBE_RATE_LIMIT_RETRIES + 1):
        try:
            data = _genai_chat(probe_body(model_id), timeout=PROBE_TIMEOUT_S)
            answer = str(data["choices"][0]["message"].get("content") or "").strip()
        except RateLimited as exc:
            if attempt < PROBE_RATE_LIMIT_RETRIES:
                time.sleep(10)
                continue
            return True, time.monotonic() - started, str(exc)[:120]
        except (
            OSError,
            RuntimeError,
            KeyError,
            IndexError,
            AttributeError,
            TypeError,
            ValueError,
        ) as exc:
            return (
                False,
                time.monotonic() - started,
                f"{type(exc).__name__}: {exc}"[:160],
            )
        return True, time.monotonic() - started, f"answered {answer[:20]!r}"
    raise AssertionError("unreachable")


def _ask_genai(prompt: str, model: str) -> str:
    """One JSON-answer chat completion; retried once on the rate limit."""
    body = {
        "model": model.split("/", 1)[-1],
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "response_format": {"type": "json_object"},
        "max_tokens": 4000,
    }
    for attempt in (1, 2):
        try:
            data = _genai_chat(body, timeout=300)
        except RateLimited:
            if attempt == 1:
                _log("dedupe: rate limited, retrying in 15s")
                time.sleep(15)
                continue
            raise RuntimeError("GenAI Studio rate limited twice") from None
        except OSError as exc:
            raise RuntimeError(f"GenAI Studio unreachable: {exc}") from exc
        return str(data["choices"][0]["message"].get("content") or "")
    raise AssertionError("unreachable")


def _push_metrics(metrics: TickMetrics) -> None:
    """One PUT to the pushgateway; never fatal, a tick is worth more than its
    metrics. Counters are read back first so they keep counting across ticks."""
    try:
        try:
            with urllib.request.urlopen(
                f"{PUSHGATEWAY_URL}/metrics", timeout=10
            ) as resp:
                previous = parse_counters(resp.read().decode())
        except OSError as exc:
            _log(f"metrics: cannot read the pushgateway ({exc}); counters restart")
            previous = {}
        text = exposition(metrics, previous)
        request = urllib.request.Request(
            f"{PUSHGATEWAY_URL}/metrics/job/{METRICS_JOB}",
            data=text.encode(),
            method="PUT",
            headers={"Content-Type": "text/plain; version=0.0.4"},
        )
        with urllib.request.urlopen(request, timeout=10):
            pass
        _log(f"metrics: pushed tick outcome={metrics.outcome} to {PUSHGATEWAY_URL}")
    except Exception as exc:
        _log(f"metrics: push failed: {exc}")


@env.task(retries=1, timeout=timedelta(minutes=15))
def dedupe(incidents: list[Incident], model: str) -> list[Group]:
    """One model call: which incidents share a root cause. A failed call
    degrades to one group per incident, never to a lost tick."""
    if len(incidents) < 2:
        return parse_groups("", incidents)
    _log(f"grouping {len(incidents)} incidents by root cause with {model}")
    try:
        reply = _ask_genai(grouping_prompt(incidents), model)
    except Exception as exc:
        _log(f"dedupe: {exc}; every incident is its own group")
        reply = ""
    groups = parse_groups(reply, incidents)
    merged = [g for g in groups if len(g.members) > 1]
    _log(f"{len(incidents)} incidents -> {len(groups)} groups ({len(merged)} merged)")
    for group in merged:
        _log(f"  {group.label}: {', '.join(group.members)}")
    return groups


# Cached on the key alone: a recurring error is analyzed once, and a fixed one
# is never re-opened. Bump the salt to re-analyze everything.
@env.task(
    # The model is ignored too: a verdict is a verdict, whichever model the
    # tick happened to run on.
    cache=flyte.Cache(behavior="auto", ignored_inputs=("evidence", "model")),
    timeout=timedelta(minutes=40),
)
def analyze(key: IncidentKey, evidence: Evidence, model: str) -> Verdict:
    _log(
        f"{key.fingerprint}: {key.workload}/{key.container} x{evidence.count}: {key.message[:120]}"
    )
    with tempfile.TemporaryDirectory(prefix="self-repair-") as tmp:
        repo = Path(tmp) / "repo"
        _clone(repo)
        context = _context(evidence, key.container)
        prompt = prompts.ANALYZE.substitute(
            incident=_describe(key, evidence),
            context=context,
            minutes=AGENT_BUDGET_MINUTES,
        )
        _log(
            f"{key.fingerprint}: prompt has {len(context.splitlines())} context line(s)"
        )
        reply = _run_agent(repo, prompt, READ_ONLY, key.fingerprint, model)
    verdict = parse_verdict(reply)
    _log(
        f"{key.fingerprint}: verdict fixable={verdict.fixable} confidence={verdict.confidence} "
        f"component={verdict.component or '-'} title={verdict.title or '-'}"
    )
    _log(f"{key.fingerprint}: reason: {verdict.reason}")
    return verdict


def _protected(changed: list[str]) -> list[str]:
    return [
        path
        for path in changed
        if path.startswith(PROTECTED_PATHS) or path.endswith(".lock")
    ]


def _python_defects(repo: Path, changed: list[str]) -> str:
    """pyflakes-level findings (undefined names, unused imports) on the
    changed Python files, ignoring the repository's exclusions: a file the
    linters skip is exactly where an undefined name slips through."""
    files = [f for f in changed if f.endswith(".py") and (repo / f).is_file()]
    if not files:
        return ""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--select",
            "F",
            "--no-cache",
            *files,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return "" if proc.returncode == 0 else (proc.stdout + proc.stderr).strip()


def _unit_tests(repo: Path, venv: Path) -> str:
    """The repository's own test failures, or "" when it passes.

    Building the environment and running the tests are separate steps so the
    two outcomes stay distinguishable: a suite that cannot be built is a
    PyPI or network problem and skips the gate, while a suite that runs and
    fails is the change being wrong. The environment is placed outside the
    checkout so nothing it writes can reach the commit.
    """
    if not (repo / "tests" / "uv.lock").is_file():
        return ""
    env_vars = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)}
    try:
        build = subprocess.run(
            ["uv", "sync", *UNIT_TEST_PROJECT],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_S,
            env=env_vars,
        )
        if build.returncode != 0:
            _log("the test environment could not be built; suite not run")
            return ""
        proc = subprocess.run(
            ["uv", "run", *UNIT_TEST_PROJECT, "--no-sync", *UNIT_TEST_ARGS],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=UNIT_TEST_TIMEOUT_S,
            env=env_vars,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"the test suite did not run ({type(exc).__name__}); gate skipped")
        return ""
    if proc.returncode == 0:
        return ""
    # pytest exits 1 for failing tests and 2-5 for its own problems
    # (collection, usage, nothing collected), which are not this change's.
    if proc.returncode != 1:
        _log(f"the test suite did not run (exit {proc.returncode}); gate skipped")
        return ""
    return (proc.stdout + proc.stderr).strip()


@env.task(timeout=timedelta(minutes=60))
def fix(key: IncidentKey, evidence: Evidence, verdict: Verdict, model: str) -> str:
    token = os.environ["GITHUB_TOKEN"]
    branch = BRANCH_PREFIX + key.fingerprint
    _log(f"{key.fingerprint}: {verdict.title} ({verdict.component}); branch {branch}")
    existing = open_pull_request(REPO, branch, token)
    if existing:
        _log(f"{key.fingerprint}: {existing} is already open, nothing to do")
        return existing
    with tempfile.TemporaryDirectory(prefix="self-repair-") as tmp:
        repo = Path(tmp) / "repo"
        _clone(repo)
        _git("checkout", "-q", "-b", branch, cwd=repo)
        prompt = prompts.FIX.substitute(
            incident=_describe(key, evidence),
            context=_context(evidence, key.container),
            title=verdict.title,
            component=verdict.component,
            reason=verdict.reason,
            plan=verdict.plan,
            minutes=AGENT_BUDGET_MINUTES,
        )
        reply = _run_agent(repo, prompt, EDIT, key.fingerprint, model)
        changed = _git("status", "--porcelain", cwd=repo).strip()
        if not changed:
            _log(f"{key.fingerprint}: the agent changed nothing")
            return ""
        _log(f"{key.fingerprint}: changed files:\n{changed}")
        paths = [line[3:].split(" -> ")[-1] for line in changed.splitlines()]
        blocked = _protected(paths)
        if blocked:
            _log(
                f"{key.fingerprint}: touches protected paths, no PR: {', '.join(blocked)}"
            )
            return ""
        diff = _git("diff", cwd=repo)
        if silences(diff):
            _log(
                f"{key.fingerprint}: the change only lowers or rewords a log message, no PR"
            )
            return ""
        if removes_error_handling(diff):
            _log(
                f"{key.fingerprint}: the change removes error handling rather than the fault, no PR"
            )
            return ""
        defects = _python_defects(repo, paths)
        if defects:
            _log(
                f"{key.fingerprint}: the change does not pass pyflakes, no PR:\n{defects[:1500]}"
            )
            return ""
        failures = _unit_tests(repo, Path(tmp) / "test-venv")
        if failures:
            _log(
                f"{key.fingerprint}: the change fails the test suite, no PR:\n{failures[-1500:]}"
            )
            return ""
        _git("add", "-A", cwd=repo)
        _git(
            "commit",
            "-q",
            "-m",
            f"{verdict.title}\n\n{verdict.reason}\n\nself-repair fingerprint {key.fingerprint}",
            cwd=repo,
        )
        # A branch left behind by a closed PR is overwritten: it is this bot's.
        _git("push", "-q", "--force", "origin", branch, cwd=repo)
        _log(f"{key.fingerprint}: pushed {branch}")
    ctx = flyte.ctx()
    run_name = (ctx.action.run_name or "") if ctx else ""
    body = pull_request_body(key, evidence, verdict, reply, run_name, model)
    url = create_pull_request(REPO, token, branch, BASE_BRANCH, verdict.title, body)
    _log(f"{key.fingerprint}: opened {url}")
    return url


async def _spawn(name: str, task: Any, *args: Any, output_type: Any) -> tuple[Any, str]:
    """Run `task` as its own run called `name`, wait for it, and return its
    output with the cache status.

    Its pod is `<name>-a0-0`. A cache hit finishes without a pod and is logged as such."""
    from flyteidl2.common import phase_pb2
    from flyteidl2.core import catalog_pb2

    started = time.monotonic()
    run = await flyte.with_runcontext(name=name).run.aio(task, *args)
    _log(f"{name}: started ({run.url})")
    await run.wait.aio(quiet=True)
    details = await run.details.aio()
    status = details.action_details.pb2.status
    phase = phase_pb2.ActionPhase.Name(status.phase).removeprefix("ACTION_PHASE_")
    cache = catalog_pb2.CatalogCacheStatus.Name(status.cache_status)
    _log(f"{name}: {phase.lower()} in {time.monotonic() - started:.0f}s, cache {cache}")
    if phase != "SUCCEEDED":
        raise RuntimeError(f"{name} ended in {phase}")
    outputs = await run.typed_outputs.aio({"o0": output_type})
    return outputs["o0"], cache


@env.task(report=True, timeout=timedelta(hours=4))
async def triage(
    trigger_time: datetime,
    # One hourly tick, plus slack for a late start (startingDeadlineSeconds)
    # and for lines Loki ingests late; repeats cost nothing, the cache has them.
    window_minutes: int = 75,
    # 60 requests/min per user at GenAI Studio; a session makes several a
    # minute, so a handful in parallel is the ceiling, not 20.
    max_incidents: int = 6,
    max_fixes: int = 2,
) -> Summary:
    if trigger_time.tzinfo is None:
        trigger_time = trigger_time.replace(tzinfo=timezone.utc)
    tick = tick_of(trigger_time)
    start = trigger_time - timedelta(minutes=window_minutes)
    window = f"{start:%H:%M:%S}..{trigger_time:%H:%M:%S}"
    _log(
        f"tick {tick} = {trigger_time:%Y-%m-%d %H:%M} UTC: window {window}, up to {max_incidents} analyses and {max_fixes} fixes"
    )
    began = time.monotonic()

    # Before anything else: is a model answering? Every later task needs one,
    # and a hung backend takes five minutes per call to say so.
    model, probes = pick_model(MODELS, _probe)
    for probe in probes:
        _log(
            f"model {probe.model}: {'answers' if probe.available else 'no answer'} "
            f"after {probe.seconds:.1f}s ({probe.detail})"
        )
    metrics = TickMetrics(
        started=trigger_time.timestamp(),
        outcome="ok",
        model=model or "",
        models=list(MODELS),
        probes=probes,
    )
    if model is None:
        note = f"No model answered ({', '.join(MODELS)}); nothing ran this tick."
        _log(note)
        await flyte.report.replace.aio(
            report_html(tick, window, [], note=note), do_flush=True
        )
        metrics.outcome = "no_model"
        metrics.duration = time.monotonic() - began
        _push_metrics(metrics)
        return Summary(
            window_start=start.isoformat(),
            window_end=trigger_time.isoformat(),
            lines=0,
            incidents=0,
            fixable=0,
            outcome="no_model",
        )
    _log(f"tick {tick} runs on {model}")
    try:
        summary = await _tick(
            tick, start, trigger_time, model, max_incidents, max_fixes, metrics
        )
    except BaseException:
        metrics.outcome = "failed"
        metrics.duration = time.monotonic() - began
        _push_metrics(metrics)
        raise
    metrics.duration = time.monotonic() - began
    _push_metrics(metrics)
    return summary


async def _tick(
    tick: str,
    start: datetime,
    trigger_time: datetime,
    model: str,
    max_incidents: int,
    max_fixes: int,
    metrics: TickMetrics,
) -> Summary:
    """The tick proper, once a model is known: watch, dedupe, analyze, fix."""
    window = f"{start:%H:%M:%S}..{trigger_time:%H:%M:%S}"
    incidents, _ = await _spawn(
        run_name("watch", tick),
        watch,
        start,
        trigger_time,
        output_type=list[Incident],
    )
    groups, _ = await _spawn(
        run_name("dedupe", tick), dedupe, incidents, model, output_type=list[Group]
    )
    raw = incidents
    incidents = [representative(group, raw) for group in groups]
    metrics.error_lines = sum(incident.evidence.count for incident in raw)
    metrics.incidents = len(raw)
    metrics.groups = len(incidents)
    _log(
        f"{len(raw)} incident(s) in {len(incidents)} group(s); up to {max_incidents} fresh "
        f"analyses, cache hits are free: {', '.join(i.key.fingerprint for i in incidents) or '-'}"
    )
    rows = {
        i.key.fingerprint: Row(
            i.key.fingerprint,
            i.key.workload,
            i.key.container,
            i.evidence.count,
            "pending",
            group=group.label,
            members=len(group.members),
        )
        for i, group in zip(incidents, groups)
    }

    async def publish() -> None:
        await flyte.report.replace.aio(
            report_html(tick, window, list(rows.values()), model=model), do_flush=True
        )

    await publish()

    async def spawn_analysis(incident: Incident) -> tuple[Verdict, str]:
        verdict, cache = await _spawn(
            run_name("analyze", tick, incident.key.fingerprint),
            analyze,
            incident.key,
            incident.evidence,
            model,
            output_type=Verdict,
        )
        rows[incident.key.fingerprint].cache = cache
        return verdict, cache

    outcomes = await analyze_within_budget(
        incidents, max_incidents, spawn_analysis, _log
    )
    for row in rows.values():
        if row.status == "pending":
            row.status = "not analyzed"
    for incident, result in outcomes:
        row = rows[incident.key.fingerprint]
        if isinstance(result, BaseException):
            row.status = "failed"
            row.reason = str(result)
        else:
            row.status = (
                "fixable"
                if result.fixable and result.confidence >= MIN_CONFIDENCE
                else "not fixable"
            )
            row.confidence, row.title, row.component, row.reason = (
                result.confidence,
                result.title,
                result.component,
                result.reason,
            )
    verdicts = [r for r in rows.values() if r.status in ("fixable", "not fixable")]
    metrics.analyses = {
        "fresh": sum(r.cache != CACHE_HIT for r in verdicts),
        "cache_hit": sum(r.cache == CACHE_HIT for r in verdicts),
        "failed": sum(r.status == "failed" for r in rows.values()),
    }
    _log(
        f"VERDICTS: {sum(r.status == 'fixable' for r in verdicts)} of {len(verdicts)} analyzed "
        f"incidents fixable here ({sum(r.cache == 'CACHE_HIT' for r in verdicts)} from cache, "
        f"{sum(r.status == 'failed' for r in rows.values())} failed)"
    )
    await publish()

    fixable = 0
    failed = 0
    pull_requests: list[str] = []
    for incident, result in outcomes:
        fingerprint = incident.key.fingerprint
        if isinstance(result, BaseException):
            failed += 1
            _log(f"{fingerprint}: analysis failed, skipping: {result}")
            continue
        verdict: Verdict = result
        if not (verdict.fixable and verdict.confidence >= MIN_CONFIDENCE):
            _log(
                f"{fingerprint}: not fixable here (fixable={verdict.fixable}, confidence={verdict.confidence})"
            )
            continue
        fixable += 1
        if len(pull_requests) >= max_fixes:
            _log(f"{fingerprint}: fixable, but {max_fixes} fix(es) already this tick")
            continue
        url, _ = await _spawn(
            run_name("fix", tick, fingerprint),
            fix,
            incident.key,
            incident.evidence,
            verdict,
            model,
            output_type=str,
        )
        if url:
            pull_requests.append(url)
            rows[fingerprint].pull_request = url
        else:
            metrics.fix_failures += 1
    await publish()
    metrics.fixable = fixable
    metrics.pull_requests = len(pull_requests)
    summary = Summary(
        window_start=start.isoformat(),
        window_end=trigger_time.isoformat(),
        lines=sum(incident.evidence.count for incident in incidents),
        incidents=len(incidents),
        fixable=fixable,
        pull_requests=pull_requests,
        model=model,
    )
    _log(
        f"tick {tick} done: {summary.lines} lines, {summary.incidents} incidents, "
        f"{len(outcomes)} analyzed, {summary.fixable} fixable, {failed} analysis failure(s), "
        f"{len(pull_requests)} PR(s) {' '.join(pull_requests)}"
    )
    return summary


if __name__ == "__main__":
    # The launcher: apps/self-repair/cronjob.yaml runs this on its schedule,
    # `kubectl create job --from=cronjob/self-repair ...` runs it by hand.
    flyte.init_from_config("config.yaml", root_dir=Path(__file__).parent)
    now = datetime.now(timezone.utc)
    run = flyte.with_runcontext(name=run_name("triage", tick_of(now))).run(
        triage, trigger_time=now
    )
    print(run.name, run.url)
