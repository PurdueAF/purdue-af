"""Flyte 2 tasks: watch Loki, analyze each error with opencode, and open a
draft pull request when the fix belongs in this repository.

`triage` orchestrates. It starts every other task as a run of its own, named
after the task, so pods read `self-repair-<task>-<tick>[-<fp>]-a0-0`
and each run shows up by name in the console."""

import asyncio
import json
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import flyte
import prompts
from triage import (
    Evidence,
    Incident,
    IncidentKey,
    Summary,
    Verdict,
    cluster,
    collect_reply,
    create_pull_request,
    describe_event,
    open_pull_request,
    parse_verdict,
    pull_request_body,
    query_loki,
)

IMAGE = "geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/self-repair:latest"
LOKI_URL = "http://loki.cms.svc.cluster.local:3100"
NAMESPACE = "cms"
REPO = "PurdueAF/purdue-af"
BASE_BRANCH = "main"
# A dash, not a slash: `self-repair/<x>` cannot be created while any branch
# named `self-repair` exists.
BRANCH_PREFIX = "self-repair-"
# A free OpenCode Zen model; OPENCODE_API_KEY (podtemplate.yaml) is optional for these.
MODEL = "opencode/big-pickle"
MIN_CONFIDENCE = 0.7
AGENT_TIMEOUT_S = 20 * 60
LOG_INCIDENTS = 20

# No web: the question is always about this repository, and a free model
# searching the web for CRD docs is how an analysis runs into its timeout.
READ_ONLY = {"edit": "deny", "bash": "deny", "webfetch": "deny", "websearch": "deny"}
EDIT = {
    "edit": "allow",
    "webfetch": "deny",
    "websearch": "deny",
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


def _run_agent(cwd: Path, prompt: str, permission: dict[str, Any], label: str) -> str:
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": MODEL,
        "permission": permission,
        "share": "disabled",
    }
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="opencode-", delete=False
    ) as handle:
        json.dump(config, handle)
    mode = "read-only" if permission is READ_ONLY else "edit"
    _log(f"{label}: opencode {MODEL} ({mode}) in {cwd}")
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
            MODEL,
            "--dir",
            str(cwd),
            prompt,
        ],
        cwd=cwd,
        env={**os.environ, "OPENCODE_CONFIG": handle.name},
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
    )

    def narrate(event: dict[str, Any]) -> None:
        line = describe_event(event)
        if line:
            _log(f"{label}: agent {line}")

    # The answer is complete at the model's final turn; the process is not
    # trusted to exit after it (opencode 1.18 lingers), so a timer bounds the
    # whole thing and the reader stops on its own.
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(AGENT_TIMEOUT_S, expire)
    timer.start()
    try:
        assert proc.stdout is not None
        reply = collect_reply(proc.stdout, narrate)
    finally:
        timer.cancel()
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
    if timed_out.is_set():
        raise RuntimeError(
            f"opencode gave no final answer within {AGENT_TIMEOUT_S}s (killed)"
        )
    raise RuntimeError(f"opencode failed after {elapsed:.0f}s: {reply.error}")


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
        f"querying {LOKI_URL} for error lines in {NAMESPACE}, {start:%H:%M:%S}..{end:%H:%M:%S} UTC"
    )
    lines = query_loki(LOKI_URL, NAMESPACE, start, end)
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


# Cached on the key alone: a recurring error is analyzed once, and a fixed one
# is never re-opened. Bump the salt to re-analyze everything.
@env.task(
    cache=flyte.Cache(behavior="auto", ignored_inputs=("evidence",)),
    timeout=timedelta(minutes=30),
)
def analyze(key: IncidentKey, evidence: Evidence) -> Verdict:
    _log(
        f"{key.fingerprint}: {key.workload}/{key.container} x{evidence.count}: {key.message[:120]}"
    )
    with tempfile.TemporaryDirectory(prefix="self-repair-") as tmp:
        repo = Path(tmp) / "repo"
        _clone(repo)
        prompt = prompts.ANALYZE.substitute(incident=_describe(key, evidence))
        reply = _run_agent(repo, prompt, READ_ONLY, key.fingerprint)
    verdict = parse_verdict(reply)
    _log(
        f"{key.fingerprint}: verdict fixable={verdict.fixable} confidence={verdict.confidence} "
        f"component={verdict.component or '-'} title={verdict.title or '-'}"
    )
    _log(f"{key.fingerprint}: reason: {verdict.reason}")
    return verdict


@env.task(timeout=timedelta(minutes=45))
def fix(key: IncidentKey, evidence: Evidence, verdict: Verdict) -> str:
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
            title=verdict.title,
            component=verdict.component,
            reason=verdict.reason,
            plan=verdict.plan,
        )
        reply = _run_agent(repo, prompt, EDIT, key.fingerprint)
        changed = _git("status", "--porcelain", cwd=repo).strip()
        if not changed:
            _log(f"{key.fingerprint}: the agent changed nothing")
            return ""
        _log(f"{key.fingerprint}: changed files:\n{changed}")
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
    body = pull_request_body(key, evidence, verdict, reply, run_name, MODEL)
    url = create_pull_request(REPO, token, branch, BASE_BRANCH, verdict.title, body)
    _log(f"{key.fingerprint}: opened {url}")
    return url


async def _spawn(name: str, task: Any, *args: Any, output_type: Any) -> Any:
    """Run `task` as its own run called `name`, wait for it, and return its output.

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
    return outputs["o0"]


@env.task(timeout=timedelta(hours=2))
async def triage(
    trigger_time: datetime,
    window_minutes: int = 20,
    max_incidents: int = 5,
    max_fixes: int = 2,
) -> Summary:
    if trigger_time.tzinfo is None:
        trigger_time = trigger_time.replace(tzinfo=timezone.utc)
    tick = tick_of(trigger_time)
    start = trigger_time - timedelta(minutes=window_minutes)
    _log(
        f"tick {tick} = {trigger_time:%Y-%m-%d %H:%M} UTC: window {start:%H:%M:%S}..{trigger_time:%H:%M:%S}, up to {max_incidents} analyses and {max_fixes} fixes"
    )

    incidents: list[Incident] = await _spawn(
        run_name("watch", tick),
        watch,
        start,
        trigger_time,
        output_type=list[Incident],
    )
    selected = incidents[:max_incidents]
    _log(
        f"analyzing {len(selected)} of {len(incidents)} incident(s): {', '.join(i.key.fingerprint for i in selected) or '-'}"
    )
    results = await asyncio.gather(
        *(
            _spawn(
                run_name("analyze", tick, incident.key.fingerprint),
                analyze,
                incident.key,
                incident.evidence,
                output_type=Verdict,
            )
            for incident in selected
        ),
        return_exceptions=True,
    )

    fixable = 0
    failed = 0
    pull_requests: list[str] = []
    for incident, result in zip(selected, results):
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
        url = await _spawn(
            run_name("fix", tick, fingerprint),
            fix,
            incident.key,
            incident.evidence,
            verdict,
            output_type=str,
        )
        if url:
            pull_requests.append(url)
    summary = Summary(
        window_start=start.isoformat(),
        window_end=trigger_time.isoformat(),
        lines=sum(incident.evidence.count for incident in incidents),
        incidents=len(incidents),
        fixable=fixable,
        pull_requests=pull_requests,
    )
    _log(
        f"tick {tick} done: {summary.lines} lines, {summary.incidents} incidents, "
        f"{summary.fixable} fixable, {failed} analysis failure(s), "
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
