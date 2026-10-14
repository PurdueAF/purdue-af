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
CORES = 8
RETRIES = 5
LOG_LINE = 240

# What an interactive session leaves to the person at the terminal.
UNATTENDED = """\
You are running unattended in a container with {cores} CPU cores and no batch \
system. Run everything here; where the methodology says SLURM, use local \
processes instead.
Nobody can answer a question before the human gate. Do not stop to ask one: \
decide, note the decision in experiment_log.md, and continue.
At the human gate, end your turn with what you would show the human and where \
the compiled note is. The reply arrives as your next message.

"""
INTERRUPTED = (
    "The session was interrupted. Check what is on disk and continue from where "
    "you left off. If you were waiting for the human, you still are: end your "
    "turn again."
)
REPLY = "The human's reply: {reply}"


@dataclass
class Analysis:
    """One analysis, as the `analysis.toml` of its directory describes it."""

    name: str
    kind: str
    repo: str
    ref: str
    files: list[str] = field(default_factory=list)
    data: str = ""


env = flyte.TaskEnvironment(
    name=NAME,
    image=IMAGE,
    # apps/jfc/podtemplate.yaml: node placement, the model credentials, the workspace.
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
        _run("pixi", "install", cwd=work)
        staged.touch()
    return work


def _turn(work: Path, config: Path, reply: str) -> list[str]:
    """The Claude Code command of the next turn: the analysis's prompt on a new
    session; on the one that exists, the human's reply, or a nudge without one."""
    session = str(uuid.uuid5(uuid.NAMESPACE_URL, str(work)))
    if any(config.glob(f"projects/*/{session}.jsonl")):
        prompt = REPLY.format(reply=reply) if reply else INTERRUPTED
        how = "--resume"
    else:
        prompt = UNATTENDED.format(cores=CORES) + (work / "prompt.md").read_text()
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


def _claude(command: list[str], work: Path, config: Path) -> str:
    """Runs one turn to its end, narrating it, and returns the agent's last message."""
    result: dict[str, Any] = {}
    with subprocess.Popen(
        command,
        cwd=work,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(config)},
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
            elif narrated := _narrate(event):
                _log(narrated)
    detail = result.get("result") or result.get("errors") or "no result"
    if result.get("api_error_status") in (401, 403):
        raise flyte.errors.NonRecoverableError(f"claude: {detail}")
    if proc.returncode or result.get("is_error", True):
        raise RuntimeError(f"claude exited {proc.returncode}: {detail}")
    _log(
        f"turn over: {result.get('num_turns')} turns, "
        f"${result.get('total_cost_usd') or 0:.2f}"
    )
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
            # A reply reaches the agent once: a retry of the run that carried it nudges.
            told = home / "replies" / ctx.action.run_name
            if reply and told.exists():
                reply = ""
            elif reply:
                told.parent.mkdir(exist_ok=True)
                told.write_text(reply)
            config = home / "claude"
            said = _claude(_turn(work, config, reply), work, config)
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
