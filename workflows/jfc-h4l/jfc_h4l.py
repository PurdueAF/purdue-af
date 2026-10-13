"""Flyte 2 run of the JFC tutorial's H→4ℓ exercise: Claude Code, headless,
takes the analysis from the physics prompt to the analysis note."""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import flyte
import flyte.errors

NAME = "jfc-h4l"
IMAGE = "geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/self-repair:latest"
# The one directory of /work a task pod mounts (apps/jfc-h4l/podtemplate.yaml).
ROOT = Path("/work/projects") / NAME

# The tutorial's "full JFC" path, at fixed commits.
JFC = ("https://github.com/jfc-mit/jfc.git", "9eecb0e0b4c95c362053ad6b121760b47379bc54")
EXERCISE = (
    "https://github.com/violatingcp/h4l_agent_test.git",
    "100d5b4636b3c5c9f3c29f4a935a3c7366c3ae90",
)
DATA = "https://s3-west.nrp-nautilus.io/transfer-bucket/h4l-data.tgz"

# The JFC specification asks for Opus in every role.
MODEL = "opus"
PERMISSION_MODE = "auto"
CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
CORES = 8
RETRIES = 5
LOG_LINE = 240

# What the tutorial leaves to the person at the terminal.
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

env = flyte.TaskEnvironment(
    name=NAME,
    image=IMAGE,
    # apps/jfc-h4l/podtemplate.yaml: node placement, the model credentials, the workspace.
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


def _data() -> Path:
    """The tutorial's ntuples, fetched once and shared by every workspace."""
    data = ROOT / "data"
    if not data.exists():
        _log(f"fetching {DATA}")
        with tempfile.TemporaryDirectory(dir=ROOT) as scratch:
            archive, _ = urllib.request.urlretrieve(DATA, Path(scratch) / "data.tgz")
            with tarfile.open(archive) as tar:
                tar.extractall(scratch, filter="data")
            (Path(scratch) / "data").rename(data)
    return data


def _prepare(home: Path) -> Path:
    """The tutorial's setup, once per workspace: JFC, an analysis scaffolded in
    it, the exercise's files there, its environment. Returns the analysis."""
    jfc, exercise = home / "jfc", home / "h4l_agent_test"
    analysis = jfc / "analyses/h4l"
    staged = home / "staged"
    if not staged.exists():
        shutil.rmtree(jfc, ignore_errors=True)
        shutil.rmtree(exercise, ignore_errors=True)
        _checkout(JFC, jfc)
        _checkout(EXERCISE, exercise)
        _run(
            sys.executable,
            "src/scaffold_analysis.py",
            "analyses/h4l",
            "--type",
            "measurement",
            cwd=jfc,
        )
        # The agent commits its work in the analysis's own repository.
        _run("git", "config", "user.name", NAME, cwd=analysis)
        _run("git", "config", "user.email", f"{NAME}@purdue-af.invalid", cwd=analysis)
        (analysis / "data").symlink_to(_data())
        (analysis / ".analysis_config").write_text(
            f"data_dir={analysis}/data\nallow={jfc}/src\n"
        )
        shutil.copytree(exercise / "docs", analysis / "docs")
        shutil.copy(exercise / "h4l_ntuplize.py", analysis)
        shutil.copy(exercise / "prompt.md", analysis)
        _run("pixi", "install", cwd=analysis)
        staged.touch()
    return analysis


def _turn(analysis: Path, config: Path, reply: str) -> list[str]:
    """The Claude Code command of the next turn: the exercise's prompt on a new
    session; on the one that exists, the human's reply, or a nudge without one."""
    session = str(uuid.uuid5(uuid.NAMESPACE_URL, str(analysis)))
    if any(config.glob(f"projects/*/{session}.jsonl")):
        prompt = REPLY.format(reply=reply) if reply else INTERRUPTED
        how = "--resume"
    else:
        prompt = UNATTENDED.format(cores=CORES) + (analysis / "prompt.md").read_text()
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


def _claude(command: list[str], analysis: Path, config: Path) -> str:
    """Runs one turn to its end, narrating it, and returns the agent's last message."""
    result: dict[str, Any] = {}
    with subprocess.Popen(
        command,
        cwd=analysis,
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
def advance(workspace: str, reply: str) -> str:
    """One stretch of the agent's work in ROOT/<workspace>, until it ends its
    turn: with the analysis note finished, or with something for a person.
    Returns what it said."""
    if not any(os.environ.get(name) for name in CREDENTIALS):
        raise flyte.errors.NonRecoverableError(
            f"none of {', '.join(CREDENTIALS)} is set: see apps/jfc-h4l/README.md"
        )
    home = ROOT / workspace
    if reply and not home.is_dir():
        raise flyte.errors.NonRecoverableError(
            f"no workspace {workspace!r} to reply to"
        )
    home.mkdir(exist_ok=True)
    ctx = flyte.ctx()
    try:
        with open(home / "lock", "w") as lock:
            # One agent per workspace: a second run on it waits for the first.
            fcntl.flock(lock, fcntl.LOCK_EX)
            analysis = _prepare(home)
            # A reply reaches the agent once: a retry of the run that carried it nudges.
            told = home / "replies" / ctx.action.run_name
            if reply and told.exists():
                reply = ""
            elif reply:
                told.parent.mkdir(exist_ok=True)
                told.write_text(reply)
            config = home / "claude"
            said = _claude(_turn(analysis, config, reply), analysis, config)
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


if __name__ == "__main__":
    # `python jfc_h4l.py [workspace [reply]]`: see README.md, Running.
    from flyte.remote import Project

    flyte.init_from_config("config.yaml", root_dir=Path(__file__).parent)
    if NAME not in {project.pb2.id for project in Project.listall()}:
        Project.create(id=NAME, name=NAME)
    name = f"{NAME}-{datetime.now(timezone.utc):%m%d-%H%M%S}"
    workspace, *words = sys.argv[1:] or [""]
    run = flyte.with_runcontext(name=name).run(
        advance, workspace=workspace or name, reply=" ".join(words)
    )
    print(run.name, run.url)
