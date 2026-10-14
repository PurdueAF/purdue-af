"""Runs JFC analyses on Flyte 2: Claude Code, headless, takes an analysis from
its physics prompt to the analysis note."""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import flyte
import flyte.errors
import jfc_dask
import jfc_profiling

NAME = "jfc"
IMAGE = "geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/self-repair:latest"
# The one directory of /work a task pod mounts (apps/jfc/podtemplate.yaml).
ROOT = Path("/work/projects") / NAME

# The specification every analysis follows, at a fixed commit.
JFC = ("https://github.com/jfc-mit/jfc.git", "9eecb0e0b4c95c362053ad6b121760b47379bc54")

# The JFC specification asks for Opus in every role.
MODEL = "opus"
PERMISSION_MODE = "auto"
CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
# Whose Dask Gateway clusters the analysis gets (jfc_dask).
GATEWAY_TOKEN = "JUPYTERHUB_API_TOKEN"
# Under the newest Python that pyroscope-io has wheels for (jfc_profiling).
PYTHON = "python<3.15"
# What jfc_profiling and jfc_dask need; the gateway also asks for prometheus-client.
PACKAGES = ("pyroscope-io", "dask-gateway", "distributed", "prometheus-client")
CORES = 8
RETRIES = 5
LOG_LINE = 240
# Seconds Claude Code has to end once told to, before it is killed.
GRACE = 30

# Claude Code's user memory: every agent and subagent reads it, at every turn.
FACILITY = """\
# The facility

These rules hold for every agent of this analysis, and over the methodology's \
where they differ.

## Run independent work at the same time

- Start subagents whose work does not depend on each other's in one message, \
as several Agent calls in the foreground: they run at the same time, and the \
results arrive when all have finished.
- What may run at once is in methodology/03a-orchestration.md §3a.5: a review \
panel's reviewers; the channels of Phases 2-3; shared calibrations beside \
Phases 2-3; within a phase, independent pieces such as plots, systematic \
variations, closure and injection tests, each on a subagent writing its own \
directory, merged before the review.
- Phases stay in order, and a review waits for the work it reviews.

## Process data on Dask Gateway

This container has {cores} cores and there is no batch system: every pass over \
the events of data or simulation runs on the facility's Dask Gateway, never in \
a local process, whatever its size. That replaces single-core, multicore, \
ProcessPoolExecutor and SLURM wherever the methodology names them. Looking at \
a file's branches or a few of its entries does not need it.

```python
from jfc_dask import client

dask = client()
```

`client()` returns a dask.distributed Client of the analysis's cluster: its \
workers run this pixi environment, grow with the work, and stop on their own. \
Split the input by file and entry range, select and fill histograms in the \
tasks, and bring back histograms, cutflows or small arrays, not events. Many \
independent fits, such as toys, are tasks too.

- Workers see /work as you do but start elsewhere: give them absolute paths.
- Workers run as another user and cannot write here: tasks return what they \
make, and the script writes it.
- A task can call what its script defines or the environment installs; send \
a module of the analysis's own with `dask.upload_file(path)`.
"""
# What an interactive session leaves to the person at the terminal.
UNATTENDED = """\
You are running unattended. Nobody can answer a question before the human \
gate. Do not stop to ask one: decide, note the decision in experiment_log.md, \
and continue.
At the human gate, end your turn with what you would show the human and where \
the compiled note is. The reply arrives as your next message.

"""
INTERRUPTED = (
    "The session was interrupted. Check what is on disk and continue from where "
    "you left off. If you were waiting for the human, you still are: end your "
    "turn again."
)
REPLY = "The human's reply: {reply}"
SPENT = (
    "Stopped: this workspace has processed its {tokens} tokens. A larger "
    "`tokens` in the analysis.toml lets it go on."
)
USAGE = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)
# The same, in the totals a turn reports at its end.
TOTALS = (
    "inputTokens",
    "cacheCreationInputTokens",
    "cacheReadInputTokens",
    "outputTokens",
)


@dataclass
class Analysis:
    """One analysis, as the `analysis.toml` of its directory describes it."""

    name: str
    kind: str
    repo: str
    ref: str
    files: list[str] = field(default_factory=list)
    data: str = ""
    scope: str = ""
    tokens: int = 0


env = flyte.TaskEnvironment(
    name=NAME,
    image=IMAGE,
    # apps/jfc/podtemplate.yaml
    pod_template=NAME,
    resources=flyte.Resources(cpu=CORES, memory="16Gi"),
)


def _log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {message}", flush=True)


def _run(*command: str, cwd: Path) -> None:
    _log(" ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def _checkout(source: tuple[str, str], dest: Path) -> None:
    url, ref = source
    dest.mkdir(parents=True)
    _run("git", "init", "--quiet", cwd=dest)
    _run("git", "fetch", "--quiet", "--depth", "1", url, ref, cwd=dest)
    _run("git", "checkout", "--quiet", "FETCH_HEAD", cwd=dest)


def _data(analysis: Analysis) -> Path:
    """The analysis's input data, fetched once per URL and shared by its workspaces."""
    version = uuid.uuid5(uuid.NAMESPACE_URL, analysis.data).hex[:8]
    data = ROOT / "data" / f"{analysis.name}-{version}"
    if not data.exists():
        data.parent.mkdir(exist_ok=True)
        _log(f"fetching {analysis.data}")
        with tempfile.TemporaryDirectory(dir=data.parent) as scratch:
            archive, _ = urllib.request.urlretrieve(
                analysis.data, Path(scratch) / "data.tgz"
            )
            with tarfile.open(archive) as tar:
                tar.extractall(scratch, filter="data")
            (Path(scratch) / "data").rename(data)
    return data


def _prepare(home: Path, analysis: Analysis) -> Path:
    """Sets a workspace up once: JFC, the analysis scaffolded in it, its prompt,
    files and data there, its environment. Returns the analysis directory."""
    spec, source = home / "jfc", home / "source"
    work = spec / "analyses" / analysis.name
    staged = home / "staged"
    if not staged.exists():
        shutil.rmtree(spec, ignore_errors=True)
        shutil.rmtree(source, ignore_errors=True)
        _checkout(JFC, spec)
        _checkout((analysis.repo, analysis.ref), source)
        _run(
            sys.executable,
            "src/scaffold_analysis.py",
            f"analyses/{analysis.name}",
            "--type",
            analysis.kind,
            cwd=spec,
        )
        # The agent commits its work in the analysis's own repository.
        _run("git", "config", "user.name", NAME, cwd=work)
        _run("git", "config", "user.email", f"{NAME}@purdue-af.invalid", cwd=work)
        if analysis.data:
            (work / "data").symlink_to(_data(analysis))
            (work / ".analysis_config").write_text(
                f"data_dir={work}/data\nallow={spec}/src\n"
            )
        for name in (*analysis.files, "prompt.md"):
            if (source / name).is_dir():
                shutil.copytree(source / name, work / name)
            else:
                shutil.copy(source / name, work / name)
        if analysis.scope:
            with open(work / "prompt.md", "a") as prompt:
                prompt.write(f"\n\n{analysis.scope}")
        _run("pixi", "add", "--no-install", PYTHON, cwd=work)
        _run("pixi", "add", "--pypi", *PACKAGES, cwd=work)
        _install(work)
        staged.touch()
    return work


def _install(work: Path) -> None:
    """Puts jfc_profiling and jfc_dask into the analysis's environment, and has
    every Python process of it start jfc_profiling, by a .pth file."""
    where = subprocess.run(
        [
            "pixi",
            "run",
            "python",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    packages = Path(where.stdout.strip().splitlines()[-1])
    for module in (jfc_profiling, jfc_dask):
        shutil.copy(str(module.__file__), packages / f"{module.__name__}.py")
    (packages / "jfc_profiling.pth").write_text(
        "import jfc_profiling; jfc_profiling.start()\n"
    )


def _turn(work: Path, config: Path, reply: str) -> list[str]:
    """The Claude Code command of the next turn: the analysis's prompt on a new
    session; on the one that exists, the human's reply, or a nudge without one."""
    session = str(uuid.uuid5(uuid.NAMESPACE_URL, str(work)))
    if any(config.glob(f"projects/*/{session}.jsonl")):
        prompt = REPLY.format(reply=reply) if reply else INTERRUPTED
        how = "--resume"
    else:
        prompt = UNATTENDED + (work / "prompt.md").read_text()
        how = "--session-id"
    return [
        "claude",
        "--print",
        prompt,
        how,
        session,
        "--model",
        MODEL,
        "--permission-mode",
        PERMISSION_MODE,
        # The scaffold links the methodology in from the JFC checkout beside it.
        "--add-dir",
        str(work.parents[1] / "src"),
        # A subagent's final reply then reaches the stream, and the count.
        "--forward-subagent-text",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def _narrate(event: dict[str, Any]) -> str:
    """A stream-json event as one log line; empty for those not worth one."""
    if event.get("type") == "system" and event.get("subtype") == "api_retry":
        return (
            f"API retry {event.get('attempt')}/{event.get('max_retries')}: "
            f"{event.get('error')}"
        )
    if event.get("type") != "assistant":
        return ""
    said = []
    for block in event.get("message", {}).get("content", []):
        if block.get("type") == "text":
            said.append(block["text"])
        elif block.get("type") == "tool_use":
            said.append(f"{block['name']} {json.dumps(block.get('input'))}")
    line = " ".join(" | ".join(said).split())[:LOG_LINE]
    # A subagent's events name the tool call that started it.
    return ("  " if event.get("parent_tool_use_id") and line else "") + line


def _processed(ledger: Path) -> int:
    """The tokens a workspace has processed so far."""
    try:
        return int(ledger.read_text()) if ledger.exists() else 0
    except ValueError:
        raise flyte.errors.NonRecoverableError(f"{ledger} holds no count") from None


def _count(ledger: Path, total: int) -> None:
    """Records `total` in `ledger`, whole or not at all."""
    draft = ledger.with_name(f"{ledger.name}.new")
    draft.write_text(str(total))
    os.replace(draft, ledger)


def _claude(
    command: list[str], work: Path, config: Path, ledger: Path, limit: int
) -> str:
    """Runs one turn to its end, narrating it, and returns the agent's last
    message. Adds the tokens of each request to `ledger`, and ends the turn
    there once the count reaches `limit`, unless that is 0."""
    result: dict[str, Any] = {}
    start = total = _processed(ledger)
    requests: dict[str | None, int] = {}
    with subprocess.Popen(
        command,
        cwd=work,
        env={
            **os.environ,
            "CLAUDE_CONFIG_DIR": str(config),
            # Tags of the profiles the analysis's scripts send.
            "JFC_ANALYSIS": work.name,
            "JFC_WORKSPACE": work.parents[2].name,
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as proc:
        for line in proc.stdout or ():
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if not isinstance(event, dict):
                _log(line.rstrip())
            elif event.get("type") == "result":
                result = event
            else:
                # A request's blocks arrive as events of one message, each with its usage.
                message = event.get("message") or {}
                usage = (
                    message.get("usage") if event.get("type") == "assistant" else None
                )
                if usage:
                    size = sum(usage.get(kind) or 0 for kind in USAGE)
                    total += size - requests.get(message.get("id"), 0)
                    requests[message.get("id")] = size
                    _count(ledger, total)
                elif event.get("subtype") == "compact_boundary":
                    # A compaction reads what it summarises and reports no usage.
                    summarised = event.get("compact_metadata") or {}
                    total += summarised.get("pre_tokens") or 0
                    _count(ledger, total)
                if narrated := _narrate(event):
                    _log(narrated)
                if limit and total >= limit:
                    proc.terminate()
                    try:
                        proc.wait(GRACE)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    _log(f"stopped at {total} of {limit} tokens")
                    return SPENT.format(tokens=limit)
    # The turn's totals also cover requests the stream does not show.
    models = (result.get("modelUsage") or {}).values()
    reported = sum(model.get(kind) or 0 for model in models for kind in TOTALS)
    if reported > total - start:
        total = start + reported
        _count(ledger, total)
    detail = result.get("result") or result.get("errors") or "no result"
    if result.get("api_error_status") in (401, 403):
        raise flyte.errors.NonRecoverableError(f"claude: {detail}")
    if proc.returncode or result.get("is_error", True):
        raise RuntimeError(f"claude exited {proc.returncode}: {detail}")
    _log(f"turn over: {result.get('num_turns')} turns, {total - start} tokens")
    return str(result.get("result") or "")


@env.task(retries=RETRIES, timeout=timedelta(hours=24))
def advance(analysis: Analysis, workspace: str, reply: str) -> str:
    """One stretch of the agent's work in ROOT/<workspace>, until it ends its
    turn: with the analysis note finished, or with something for a person.
    Returns what it said."""
    if not any(os.environ.get(name) for name in CREDENTIALS):
        raise flyte.errors.NonRecoverableError(
            f"none of {', '.join(CREDENTIALS)} is set: see apps/jfc/README.md"
        )
    if not os.environ.get(GATEWAY_TOKEN):
        raise flyte.errors.NonRecoverableError(
            f"{GATEWAY_TOKEN} is not set: see apps/jfc/README.md"
        )
    ctx = flyte.ctx()
    home = ROOT / workspace
    # A run makes only the workspace named after it; any other must exist.
    if workspace != ctx.action.run_name and not home.is_dir():
        raise flyte.errors.NonRecoverableError(f"no workspace {workspace!r}")
    home.mkdir(exist_ok=True)
    try:
        with open(home / "lock", "w") as lock:
            # One agent per workspace: a second run on it waits for the first.
            fcntl.flock(lock, fcntl.LOCK_EX)
            work = _prepare(home, analysis)
            config, ledger = home / "claude", home / "tokens"
            if analysis.tokens and _processed(ledger) >= analysis.tokens:
                raise flyte.errors.NonRecoverableError(
                    SPENT.format(tokens=analysis.tokens)
                )
            # A reply reaches the agent once: a retry of the run that carried it nudges.
            told = home / "replies" / ctx.action.run_name
            if reply and told.exists():
                reply = ""
            elif reply:
                told.parent.mkdir(exist_ok=True)
                told.write_text(reply)
            config.mkdir(exist_ok=True)
            (config / "CLAUDE.md").write_text(FACILITY.format(cores=CORES))
            turn = _turn(work, config, reply)
            said = _claude(turn, work, config, ledger, analysis.tokens)
            (home / "said.md").write_text(said)
    except flyte.errors.NonRecoverableError:
        raise
    except RuntimeError as failed:
        # The backend restarts a failed attempt at once; a provider's usage limit lasts hours.
        if ctx.attempt_number < RETRIES:
            pause = min(15 * 2**ctx.attempt_number, 240)
            _log(f"{failed}; next attempt in {pause} min")
            time.sleep(pause * 60)
        raise
    print(said, flush=True)
    return said


def launch(directory: str, workspace: str = "", *reply: str) -> None:
    """Starts a run of the analysis in `directory` (README.md, Running)."""
    from flyte.remote import Project

    with open(Path(directory) / "analysis.toml", "rb") as file:
        analysis = Analysis(**tomllib.load(file))
    flyte.init_from_config("config.yaml", root_dir=Path(__file__).parent)
    if NAME not in {project.pb2.id for project in Project.listall()}:
        Project.create(id=NAME, name=NAME)
    name = f"{NAME}-{analysis.name}-{datetime.now(timezone.utc):%m%d-%H%M%S}"
    run = flyte.with_runcontext(name=name).run(
        advance, analysis=analysis, workspace=workspace or name, reply=" ".join(reply)
    )
    print(run.name, run.url)


if __name__ == "__main__":
    launch(*sys.argv[1:])
