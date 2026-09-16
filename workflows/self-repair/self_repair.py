"""Flyte 2 tasks: watch Loki, analyze each error with opencode, and open a
draft pull request when the fix belongs in this repository."""

import asyncio
import json
import os
import subprocess
import tempfile
import threading
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
BRANCH_PREFIX = "self-repair/"
# A free OpenCode Zen model; OPENCODE_API_KEY (podtemplate.yaml) is optional for these.
MODEL = "opencode/big-pickle"
MIN_CONFIDENCE = 0.7
AGENT_TIMEOUT_S = 20 * 60

READ_ONLY = {"edit": "deny", "bash": "deny", "webfetch": "deny"}
EDIT = {
    "edit": "allow",
    "webfetch": "deny",
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

every_15_minutes = flyte.Trigger(
    "every-15-minutes",
    flyte.Cron("*/15 * * * *"),
    inputs={"trigger_time": flyte.TriggerTime},
    description="Triage the error lines of the last 20 minutes in cms",
)


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return proc.stdout


def _clone(dest: Path) -> None:
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


def _run_agent(cwd: Path, prompt: str, permission: dict[str, Any]) -> str:
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
        stderr=subprocess.DEVNULL,
        text=True,
    )
    # The answer is complete at the model's final turn; the process is not
    # trusted to exit after it (opencode 1.18 lingers), so a timer bounds the
    # whole thing and the reader stops on its own.
    timer = threading.Timer(AGENT_TIMEOUT_S, proc.kill)
    timer.start()
    try:
        assert proc.stdout is not None
        reply = collect_reply(proc.stdout)
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if reply.error:
        raise RuntimeError(f"opencode failed: {reply.error}")
    if not reply.finished:
        raise RuntimeError(f"opencode gave no final answer within {AGENT_TIMEOUT_S}s")
    return reply.text


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
    lines = query_loki(LOKI_URL, NAMESPACE, start, end)
    incidents = cluster(lines)
    print(f"{len(lines)} error lines in {len(incidents)} distinct incident(s)")
    return incidents


# Cached on the key alone: a recurring error is analyzed once, and a fixed one
# is never re-opened. Bump the salt to re-analyze everything.
@env.task(
    cache=flyte.Cache(behavior="auto", ignored_inputs=("evidence",)),
    timeout=timedelta(minutes=30),
)
def analyze(key: IncidentKey, evidence: Evidence) -> Verdict:
    with tempfile.TemporaryDirectory(prefix="self-repair-") as tmp:
        repo = Path(tmp) / "repo"
        _clone(repo)
        reply = _run_agent(
            repo,
            prompts.ANALYZE.substitute(incident=_describe(key, evidence)),
            READ_ONLY,
        )
    verdict = parse_verdict(reply)
    print(
        f"{key.fingerprint}: fixable={verdict.fixable} confidence={verdict.confidence} {verdict.title}"
    )
    return verdict


@env.task(timeout=timedelta(minutes=45))
def fix(key: IncidentKey, evidence: Evidence, verdict: Verdict) -> str:
    token = os.environ["GITHUB_TOKEN"]
    branch = BRANCH_PREFIX + key.fingerprint
    existing = open_pull_request(REPO, branch, token)
    if existing:
        print(f"{branch}: {existing} is already open")
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
        reply = _run_agent(repo, prompt, EDIT)
        if not _git("status", "--porcelain", cwd=repo).strip():
            print(f"{key.fingerprint}: the agent changed nothing")
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
    ctx = flyte.ctx()
    run_name = (ctx.action.run_name or "") if ctx else ""
    body = pull_request_body(key, evidence, verdict, reply, run_name, MODEL)
    url = create_pull_request(REPO, token, branch, BASE_BRANCH, verdict.title, body)
    print(f"{key.fingerprint}: opened {url}")
    return url


@env.task(triggers=every_15_minutes, timeout=timedelta(hours=2))
async def triage(
    trigger_time: datetime,
    window_minutes: int = 20,
    max_incidents: int = 5,
    max_fixes: int = 2,
) -> Summary:
    if trigger_time.tzinfo is None:
        trigger_time = trigger_time.replace(tzinfo=timezone.utc)
    start = trigger_time - timedelta(minutes=window_minutes)
    incidents = await watch.aio(start, trigger_time)
    selected = incidents[:max_incidents]
    verdicts = await asyncio.gather(
        *(analyze.aio(incident.key, incident.evidence) for incident in selected)
    )
    fixable = 0
    pull_requests: list[str] = []
    for incident, verdict in zip(selected, verdicts):
        if not (verdict.fixable and verdict.confidence >= MIN_CONFIDENCE):
            continue
        fixable += 1
        if len(pull_requests) >= max_fixes:
            continue
        with flyte.group(f"fix-{incident.key.fingerprint}"):
            url = await fix.aio(incident.key, incident.evidence, verdict)
        if url:
            pull_requests.append(url)
    return Summary(
        window_start=start.isoformat(),
        window_end=trigger_time.isoformat(),
        lines=sum(incident.evidence.count for incident in incidents),
        incidents=len(incidents),
        fixable=fixable,
        pull_requests=pull_requests,
    )


if __name__ == "__main__":
    flyte.init_from_config("config.yaml", root_dir=Path(__file__).parent)
    run = flyte.run(triage, trigger_time=datetime.now(timezone.utc))
    print(run.url)
    run.wait()
    print(run.outputs())
