"""Runs JFC analyses on Flyte 2: Claude Code, headless, takes an analysis from
its physics prompt to the analysis note."""

import fcntl
import json
import os
import re
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
from typing import Any, Iterator

import flyte
import flyte.errors

# self-repair's pacing proxy for GenAI Studio; the launcher bundles both workflows' files.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "self-repair"))
from genai_proxy import Handler, Proxy  # noqa: E402

NAME = "jfc"
IMAGE = "geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/self-repair:latest"
# The one directory of /work a task pod mounts (apps/jfc/podtemplate.yaml).
ROOT = Path("/work/projects") / NAME

# The specification every analysis follows, at a fixed commit.
JFC = ("https://github.com/jfc-mit/jfc.git", "9eecb0e0b4c95c362053ad6b121760b47379bc54")

# Served by Purdue GenAI Studio; apps/jfc/podtemplate.yaml supplies the key.
MODEL = "qwen3.8:27b"
# The reasoning effort asked of it; GenAI Studio takes xhigh, medium or low.
EFFORT = "xhigh"
# Requests a minute: under GenAI Studio's limit for a key, which self-repair shares.
RPM = 15
# Its deployed context in tokens, of which a request keeps OUTPUT for the reply.
CONTEXT = 65536
OUTPUT = 12000
# The tokens of CONTEXT the tool results of one step may take between them.
RESULTS = 14000
CUT = "\n[Cut here: the rest of {what} does not fit your context. Get it in parts.]"
# Nobody is there to ask, and auto mode needs Anthropic's own service: file edits
# are accepted and these tools allowed outright. The pod is the boundary.
PERMISSION_MODE = "acceptEdits"
ALLOWED_TOOLS = "Bash,WebFetch"
# Whichever model Claude Code or the JFC specification names, MODEL answers.
SETTINGS = {
    "ANTHROPIC_MODEL": MODEL,
    "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
    "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL,
    "CLAUDE_CODE_SUBAGENT_MODEL": MODEL,
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(OUTPUT),
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
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
    # apps/jfc/podtemplate.yaml: node placement, the GenAI Studio key, the workspace.
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
        "--effort",
        EFFORT,
        "--permission-mode",
        PERMISSION_MODE,
        "--allowedTools",
        ALLOWED_TOOLS,
        # Web search runs on Anthropic's side, which this model has none of.
        "--disallowedTools",
        "WebSearch",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


WORD = re.compile(r"[^\W\d_]+")
BLANK = re.compile(r"\s+")
BETWEEN_WORDS = re.compile(r"(?<=[^\W\d_]) (?=[^\W\d_])")


def _tokens(text: str) -> int:
    """An upper estimate of what MODEL makes of `text`, after its counts of
    prose, code, logs and tables of numbers: a token a word and one more
    every six letters of it, a token any other character, a run of blanks
    counted once, and the space between two words not at all."""
    words, blanks = WORD.findall(text), BLANK.findall(text)
    other = len(text) - sum(map(len, words)) - sum(map(len, blanks))
    worded = sum(1 + (len(word) - 1) // 6 for word in words)
    return worded + other + len(blanks) - len(BETWEEN_WORDS.findall(text))


def _files(messages: list[dict[str, Any]]) -> dict[str, str]:
    """The file each Read of a conversation was of, by the id of its call."""
    files = {}
    for message in messages:
        content = message["content"]
        if message["role"] != "assistant" or isinstance(content, str):
            continue
        for block in content:
            given = block.get("input")
            if block["type"] == "tool_use" and isinstance(given, dict):
                if isinstance(given.get("file_path"), str):
                    files[block["id"]] = given["file_path"]
    return files


def _order_results(messages: list[dict[str, Any]]) -> int:
    """Puts the tool results of each message in the order of the calls they
    answer, and returns how many messages it changed. Claude Code lists them
    as they finish; MODEL is shown no ids and takes them by position."""
    changed, calls = 0, {}
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            continue
        if message["role"] == "assistant":
            asked = [block["id"] for block in content if block["type"] == "tool_use"]
            calls = {call: at for at, call in enumerate(asked)}
        elif message["role"] == "user":
            places = [at for at, b in enumerate(content) if b["type"] == "tool_result"]
            results = [content[at] for at in places]
            ordered = sorted(
                results, key=lambda b: calls.get(b.get("tool_use_id"), len(calls))
            )
            if ordered != results:
                for at, block in zip(places, ordered):
                    content[at] = block
                changed += 1
    return changed


def _cut_results(messages: list[dict[str, Any]], files: dict[str, str]) -> int:
    """Cuts the tool results of each message to RESULTS tokens between them
    and returns how many it cut. The smallest is served first, so only a
    result larger than an even share loses its end."""
    cut = 0
    for message in messages:
        content = message["content"]
        if message["role"] != "user" or isinstance(content, str):
            continue
        texts: list[tuple[dict[str, Any], str, str]] = []
        for block in content:
            inner = block.get("content") if block["type"] == "tool_result" else None
            what = files.get(block.get("tool_use_id"), "this result")
            if isinstance(inner, str):
                texts.append((block, "content", what))
            elif isinstance(inner, list):
                texts += [
                    (part, "text", what) for part in inner if part["type"] == "text"
                ]
        sizes = [_tokens(holder[key]) for holder, key, _ in texts]
        left = RESULTS
        for placed, at in enumerate(sorted(range(len(texts)), key=sizes.__getitem__)):
            holder, key, what = texts[at]
            share, size, text = left // (len(texts) - placed), sizes[at], holder[key]
            while size > share:
                text = text[: len(text) * share // size]
                size = _tokens(text)
            left -= size
            if text != holder[key]:
                holder[key] = text + CUT.format(what=what)
                cut += 1
    return cut


def _move_images(messages: list[dict[str, Any]], files: dict[str, str]) -> int:
    """Moves each image of a tool result behind the results of its message,
    named there and in its result by the file it was read from, and returns
    how many it moved."""
    moved = 0
    for message in messages:
        content = message["content"]
        if message["role"] != "user" or isinstance(content, str):
            continue
        images: list[tuple[str, dict[str, Any]]] = []
        for block in content:
            inner = block.get("content") if block["type"] == "tool_result" else None
            if not isinstance(inner, list):
                continue
            file = files.get(block.get("tool_use_id"))
            block["content"] = []
            for part in inner:
                if part["type"] != "image":
                    block["content"].append(part)
                    continue
                name = f"Image {len(images) + 1}" + (f" ({file})" if file else "")
                images.append((name, part))
                shown = f"[{name}, shown after the tool results]"
                block["content"].append({"type": "text", "text": shown})
        for name, image in images:
            content += [{"type": "text", "text": f"{name}:"}, image]
        moved += len(images)
    return moved


def _payloads(node: Any) -> Iterator[str]:
    """The base64 of every image and document under `node`."""
    if isinstance(node, list):
        for value in node:
            yield from _payloads(value)
    elif isinstance(node, dict):
        source = node.get("source")
        if isinstance(source, dict) and isinstance(source.get("data"), str):
            yield source["data"]
        for value in node.values():
            yield from _payloads(value)


class Messages(Handler):
    """GenAI Studio's Messages route as Claude Code needs it. The tool
    results of one step go in the order of their calls, and are cut to what
    leaves a conversation compactable. An image inside a tool result, where
    Read puts one, is dropped there, so it moves out. A prompt that leaves
    the reply no room is refused in Anthropic's words, on which Claude Code
    compacts the conversation and goes on."""

    prompt = 0
    limit = CONTEXT

    def ask(self, body: bytes) -> bytes:
        self.prompt, self.limit = len(body), CONTEXT
        try:
            request = json.loads(body)
            messages = request["messages"]
            files = _files(messages)
            changed = _order_results(messages) + _cut_results(messages, files)
            if changed + _move_images(messages, files):
                body = json.dumps(request, ensure_ascii=False).encode()
            self.limit -= int(request.get("max_tokens") or 0)
            carried = sum(map(_tokens, _payloads(messages)))
            self.prompt = _tokens(body.decode()) - carried
        except (ValueError, LookupError, TypeError, AttributeError) as error:
            _log(f"a request went on as it came: {error!r}")
        return body

    def refusal(self, status: int, body: bytes) -> bytes:
        if status != 400 or b"ContextWindowExceededError" not in body:
            return body
        size = max(self.prompt, self.limit + 1)
        message = f"prompt is too long: {size} tokens > {self.limit} maximum"
        error = {"type": "invalid_request_error", "message": message}
        return json.dumps({"type": "error", "error": error}).encode()


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
    with Proxy(rpm=RPM, handler=Messages) as genai:
        environment = {
            **os.environ,
            **SETTINGS,
            "ANTHROPIC_BASE_URL": f"{genai.url}/api",
            "CLAUDE_CONFIG_DIR": str(config),
        }
        with subprocess.Popen(
            command,
            cwd=work,
            env=environment,
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
    flyte.init_from_config(
        "jfc/config.yaml", root_dir=Path(__file__).resolve().parents[1]
    )
    if NAME not in {project.pb2.id for project in Project.listall()}:
        Project.create(id=NAME, name=NAME)
    name = f"{NAME}-{analysis.name}-{datetime.now(timezone.utc):%m%d-%H%M%S}"
    run = flyte.with_runcontext(name=name).run(
        advance, analysis=analysis, workspace=workspace or name, reply=" ".join(reply)
    )
    print(run.name, run.url)


if __name__ == "__main__":
    launch(*sys.argv[1:])
