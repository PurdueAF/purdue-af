"""Runs analyses on Flyte 2 with the JFC methodology: OpenCode, headless, on
the LLM proxy's models, takes an analysis from its physics prompt to the
analysis note."""

import fcntl
import functools
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import af_dask
import af_profiling
import af_ray
import flyte
import flyte.errors

NAME = "agentic-analysis"
IMAGE = (
    "geddes2-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/agentic-analysis:latest"
)
# The workload account's volume, where the task pods and its Dask and Ray clusters mount it (apps/agentic-analysis/podtemplate.yaml).
ROOT = Path("/") / NAME

# The JFC methodology every analysis follows, at a fixed commit.
JFC = ("https://github.com/jfc-mit/jfc.git", "9eecb0e0b4c95c362053ad6b121760b47379bc54")

# Environment variables of the task pods: the LLM proxy's address and its clients' token.
PROXY = "AF_LLM_PROXY"
TOKEN = "AF_LLM_TOKEN"
# Each JFC role (src/agents/<role>.md): the proxy's role it runs on (apps/agentic-analysis/models.toml), and what the orchestrator is told it does.
AGENTS = {
    "executor": (
        "executor",
        "Carries out a phase or one piece of it: plan.md first, then scripts and figures, the artifact last.",
    ),
    "fixer": (
        "executor",
        "Makes the smallest fixes that answer an arbiter's ITERATE findings or a regression ticket.",
    ),
    "note_writer": (
        "note-writer",
        "Writes the phase-stamped analysis note from the artifacts and results JSON: prose only.",
    ),
    "typesetter": (
        "typesetter",
        "Builds the analysis note's PDF with pandoc, postprocess_tex.py and tectonic: layout only.",
    ),
    "physics_reviewer": (
        "physics-reviewer",
        "Reviews the physics as a senior collaboration member would, without the methodology or conventions.",
    ),
    "critical_reviewer": (
        "critical-reviewer",
        "Finds every flaw of correctness and completeness, against the methodology and conventions.",
    ),
    "constructive_reviewer": (
        "constructive-reviewer",
        "Strengthens the analysis' clarity, validation and presentation, and flags real errors as Category A.",
    ),
    "plot_validator": (
        "plot-validator",
        "Lints plotting code and inspects the rendered figures it is given; red flags are Category A.",
    ),
    "bibtex_validator": (
        "bibtex",
        "Checks that every citation resolves to a real DOI, arXiv or INSPIRE record.",
    ),
    "rendering_reviewer": (
        "critical-reviewer",
        "Phase 5: compiles the analysis note to PDF and inspects its rendering, not its physics.",
    ),
    "arbiter": (
        "arbiter",
        "Adjudicates the reviews into PASS, ITERATE or ESCALATE, and raises what every reviewer missed.",
    ),
    "investigator": (
        "arbiter",
        "On a regression, traces its impact to the phase it started in and writes REGRESSION_TICKET.md; fixes nothing.",
    ),
}
# OpenCode's waits on the proxy in ms: for headers, between keep-alives, and for a request the proxy holds.
WAITS = {"headerTimeout": 60_000, "chunkTimeout": 90_000, "timeout": 3 * 3_600_000}
# OpenCode's ceiling on a reply's tokens: it compacts limit.output below a model's context, or this far for 0.
OUTPUT_MAX = 32_000
# The most bytes a token of a request's text takes, as OpenCode sends it.
TOKEN_BYTES = 5
# Nothing asks, as nobody answers ("*" first: the last rule that matches wins); no agent edits what OpenCode loads.
PERMISSION = {
    "*": "allow",
    "read": "allow",
    "external_directory": "allow",
    "doom_loop": "allow",
    "question": "deny",
    "edit": {
        "*": "allow",
        "*.opencode/*": "deny",
        "*opencode.json*": "deny",
        "*AGENTS.md": "deny",
        "*CONTEXT.md": "deny",
    },
}
# What OpenCode would load from the analysis over the runner's configuration, and over JFC's CLAUDE.md files.
CONFIGS = (".opencode", "opencode.json", "opencode.jsonc", "AGENTS.md", "CONTEXT.md")
PLUGIN = Path(__file__).with_name("af_opencode.js")
# Where commands keep their configuration, data, state and caches; the turn's own environment moves them.
XDG = {
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_STATE_HOME": ".local/state",
    "XDG_CACHE_HOME": ".cache",
}
# The environment variable that marks every process of a turn, its commands' own sessions included.
MARK = "AF_TURN"
# Subagents that run at once; another `task` call waits for a seat.
SEATS = 8
# Every analysis's Python: TensorFlow, which zfit needs, has no wheels for 3.14, and on 3.11 the Dask image's user packages shadow the environment's.
PYTHON = "3.12"
# Only conda-forge builds xrootd's bindings.
CONDA = (f"python={PYTHON}", "xrootd")
# The helpers' and XRootD's; the gateway also asks for prometheus-client.
PACKAGES = (
    "pyroscope-io",
    "dask-gateway",
    "distributed",
    "prometheus-client",
    "fsspec-xrootd",
)
# What has every Python process of an environment start af_profiling.
HOOK = "af_profiling.pth"
CORES = 8
RETRIES = 5
# Turns a run resumes after a reply that ran out of output tokens.
RESUMES = 3
# Why a step ends that its reply goes on from, or no step: any other ends the reply.
ONGOING = ("", "tool-calls", "unknown")
LOG_LINE = 240
# Seconds OpenCode has to end once told to, before it is killed.
GRACE = 30
# Seconds within which the plugin must have loaded, or the turn ends.
LOADED = 60
# The longest a foreground command may run: a pass over the data is one.
COMMAND_HOURS = 24
# A command that sets no timeout of its own ends after this.
COMMAND_MINUTES = 10
# Seconds of silence that end a turn: longer than a request or a command, each of which the plugin notes as it starts.
STALL = (COMMAND_HOURS + 1) * 3600
POLL = 1.0

# Every agent and subagent reads it, at every turn.
FACILITY = """\
# The facility

These rules hold for every agent of this analysis, and over the methodology's \
where they differ.

## Subagents

- Only the orchestrator starts subagents, with the `task` tool. Its \
`subagent_type` is the name of the role's definition in `agents/` without \
`.md`, as `note_writer`; an ad-hoc subagent is an `executor`.
- A subagent, which has no `task` tool, does the task the orchestrator gives \
it, in its first message and in any it is resumed with, and nothing beyond \
it, then ends with its result. Where the analysis's CLAUDE.md speaks to the \
orchestrator, a subagent is not it: only the orchestrator moves the analysis \
on to a next step, phase or gate.
- Where the methodology has a subagent start another or delegate a part of \
its work, that subagent does the work itself, or ends with what it asks the \
orchestrator to start.
- The facility sets each role's model: `task` takes none, and the \
methodology's choices of model do not apply.
- A subagent runs until it ends, never in the background, so there is none to \
watch. The orchestrator receives only its last message: the files it wrote, \
its verdict and what is open. A failed one is resumed with its `task_id`, or \
started afresh.
- Plan mode is writing plan.md before anything else. The facility keeps \
every session's transcript, so none is exported.

## Run independent work at the same time

- Start subagents whose work does not depend on each other's in one message, \
as several `task` calls: they run at the same time, as many as the facility \
has seats for, the others as seats free up, and the results arrive when all \
have finished.
- What may run at once is in methodology/03a-orchestration.md §3a.5: a review \
panel's reviewers; the channels of Phases 2-3; shared calibrations beside \
Phases 2-3; within a phase, independent pieces such as plots, systematic \
variations, MVA trainings, closure and injection tests. The orchestrator \
splits them, each to an executor writing its own directory, merged by \
another before the review.
- Phases stay in order, and a review waits for the work it reviews.

## Commands and where files are

- A command that sets no `timeout` is ended after {minutes} minutes. Give one \
that runs longer, as a pass over the data, a `timeout` long enough, up to \
{hours} hours.
- An analysis given input files has them in its `data/`, a link to where \
they were fetched: `ls data/` lists them, and a search started above `data/` \
passes over the link unless it is `find -L`. One without `data/` reads its \
inputs as its prompt says.
- Never search for a file from `/`, `/cvmfs` or `{root}`, whether it is the \
analysis's, a command or a font: they are too large to search, and such a \
search runs until its command is ended. Search the analysis's directory and \
the JFC checkout it is in for its files.
- A command is on the `PATH`, as `command -v NAME` shows, or in the \
analysis's pixi environment, `.pixi/envs/default`, as `pixi run which NAME` \
shows; one in neither is not installed, and `pixi add NAME` adds it. Tectonic \
keeps the TeX files and fonts it fetches in `~/.cache/tectonic`.
- The analysis's pixi environment stays on Python {python}: its tools and the \
facility's Dask run on it, and another version rebuilds the environment and \
splits the analysis's results across two interpreters.

## Files, figures and PDFs

- A reply's length is bounded: write a large file in parts. `read` returns a \
long file in pieces: read on with `offset` to its end.
- `read` shows a PNG to the roles that see images, a limited number per \
session, each of a limited size; a refused read says why. Review figures \
from their own PNGs, a few related ones per plot validator, each batch to a \
fresh one.
- Never `read` a PDF: render its pages with `pdftoppm -r 150 -png`, and use \
`pdftotext` and `pdfinfo` for its text, unresolved references and page count. \
Look at pages only for layout, captions and tables.
- A subagent looks only at the figures or pages its prompt names. Where the \
methodology has one agent look at every figure or page, as it has the \
reviewers, the typesetter and the rendering reviewer, the orchestrator splits \
the figures by name and the pages by range into batches, gives each batch to a \
fresh session of that role, and merges their findings before the arbiter \
sees them.

## Review a figure by measuring it first

- The orchestrator gives each figure it sends for review its intended \
content: dataset, luminosity, axis ranges, binning, overflow handling and \
where a signal is expected.
- Whoever reviews a figure first copies out its axis titles, experiment label \
and luminosity label as printed, then measures before judging: the lower \
panel's y-range and the markers or error bars outside it; the first and last \
x-axis ticks, and whether the last bin is drawn whole; whether the data points \
of each panel carry error bars; each legend entry against the colour and \
marker drawn for it. The findings follow from those measurements and the \
intended content.
- Error bars smaller than their marker are not missing.

## Read CMS data through XCache

Read CMS datasets with uproot from \
`root://xcache.cms.rcac.purdue.edu//store/...`, the dataset's file names after \
the prefix. XCache asks for a grid proxy: this container and the analysis's \
Dask workers hold the facility's, with which DAS answers too \
(`/cvmfs/cms.cern.ch/common/dasgoclient`). Never copy the proxy anywhere. The \
environment has xrootd and fsspec-xrootd.

## Process data on Dask Gateway

This container has {cores} cores and there is no batch system: every pass over \
the events of data or simulation runs on the facility's Dask Gateway, never in \
a local process, whatever its size. That replaces single-core, multicore, \
ProcessPoolExecutor and SLURM wherever the methodology names them. Looking at \
a file's branches or a few of its entries does not need it.

```python
from af_dask import client

dask = client(workers=50)
```

`client()` returns a dask.distributed Client of the analysis's cluster: its \
workers run this pixi environment, grow with the work up to `workers`, and \
stop on their own. For a large cluster, make the workers larger rather than \
more, as in `client(workers=250, cores=4, memory=16)`; the script that starts \
the cluster sets their size. The gateway's limits are in \
https://analysis-facility.physics.purdue.edu/guide-dask-gateway/. Split the \
input by file and entry range, select and fill histograms in the tasks, and \
bring back histograms, cutflows or small arrays, not events. Many independent \
fits, such as toys, are tasks too.

- Workers start only once work is submitted, so a check of the cluster \
submits a task, as `dask.submit(lambda: 1).result(timeout=300)`, instead of \
waiting for workers.
- A pass runs in the foreground, with a `timeout` long enough for it.
- Workers see {root} as you do, and no other storage but /cvmfs, but start \
elsewhere: give them absolute paths.
- Workers run as you and write where you do.
- A task can call what its script defines or the environment installs; send \
a module of the analysis's own with `dask.upload_file(path)`.

## Train on GPUs with Ray

A model that trains on GPUs trains on the facility's Ray, not here:

```python
from af_ray import client

jobs = client(max_workers=4)
job = jobs.submit_job(entrypoint="python /absolute/path/to/train.py")
```

`client()` returns a Ray JobSubmissionClient whose jobs run in this pixi \
environment, with Ray and PyTorch added to it as \
https://analysis-facility.physics.purdue.edu/guide-ray-train/ says. Its \
keyword arguments are the settings of that guide without `af-`: \
`max_workers=4` is `af-max-workers`. Its scripts show data-parallel training \
that survives losing GPUs.

- Ray's pods see {root} as you do, and no other storage but /cvmfs: give the \
entrypoint absolute paths, without a working_dir.
- They run as you, and hold no grid proxy: they read what Dask's tasks wrote, \
and write checkpoints, models and outputs in the workspace.
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


@dataclass
class Analysis:
    """One analysis, as the `analysis.toml` of its directory describes it."""

    name: str
    kind: str
    repo: str = ""
    ref: str = ""
    files: list[str] = field(default_factory=list)
    data: str = ""
    scope: str = ""
    # Without a repo: prompt.md and the files, from the analysis's directory.
    sources: dict[str, str] = field(default_factory=dict)


env = flyte.TaskEnvironment(
    name=NAME,
    image=IMAGE,
    # apps/agentic-analysis/podtemplate.yaml
    pod_template=NAME,
    resources=flyte.Resources(cpu=CORES, memory="16Gi"),
    include=(PLUGIN.name,),
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
        if analysis.repo:
            _checkout((analysis.repo, analysis.ref), source)
        for name, text in analysis.sources.items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text(text, encoding="utf-8")
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
        _run("pixi", "add", "--no-install", *CONDA, cwd=work)
        _run("pixi", "add", "--pypi", *PACKAGES, cwd=work)
        staged.touch()
    return work


def _helpers(local: Path) -> Path:
    """Writes what every environment of an analysis holds into `local`, for the
    plugin to put there: af_profiling, af_dask, af_ray and HOOK. Returns the
    directory."""
    helpers = local / "helpers"
    helpers.mkdir()
    for module in (af_profiling, af_dask, af_ray):
        (helpers / f"{module.__name__}.py").write_text(
            Path(str(module.__file__)).read_text()
        )
    (helpers / HOOK).write_text("import af_profiling; af_profiling.start()\n")
    return helpers


def _roles() -> dict[str, dict[str, Any]]:
    """The LLM proxy's roles: each one's context, where it compacts, and the
    images a request of it takes."""
    asked = urllib.request.Request(
        f"{os.environ[PROXY]}/config",
        headers={"Authorization": f"Bearer {os.environ[TOKEN]}"},
    )
    try:
        with urllib.request.urlopen(asked, timeout=60) as answer:
            roles: dict[str, dict[str, Any]] = json.load(answer)
            return roles
    except urllib.error.HTTPError as refused:
        if refused.code in (401, 403):
            raise flyte.errors.NonRecoverableError(
                f"the LLM proxy refuses {TOKEN}: {refused}"
            ) from None
        raise RuntimeError(f"the LLM proxy's roles: {refused}") from None
    except (OSError, ValueError) as failed:
        raise RuntimeError(f"the LLM proxy's roles: {failed}") from None


def _config(
    roles: dict[str, dict[str, Any]], facility: Path, work: Path
) -> dict[str, Any]:
    """OpenCode's configuration: the proxy as its one provider, with a model
    per role; the orchestrator and an agent per JFC role on their roles' models;
    FACILITY and the analysis's CLAUDE.md as every agent's instructions."""
    wanted = {"orchestrator", "compaction", *(role for role, _ in AGENTS.values())}
    if missing := sorted(wanted - roles.keys()):
        raise flyte.errors.NonRecoverableError(
            f"the LLM proxy has no role {', '.join(missing)}"
        )
    for name, role in roles.items():
        if not 0 <= role["context"] - role["compact_at"] <= OUTPUT_MAX:
            raise flyte.errors.NonRecoverableError(
                f"the LLM proxy's role {name} compacts at {role['compact_at']} of "
                f"{role['context']} tokens; OpenCode compacts within {OUTPUT_MAX} of a context"
            )
    models = {
        name: {
            "name": name,
            "tool_call": True,
            "modalities": {
                "input": ["text", "image"] if role["images"] else ["text"],
                "output": ["text"],
            },
            # OpenCode compacts at context - output; 0 is OUTPUT_MAX.
            "limit": {
                "context": role["context"],
                "output": role["context"] - role["compact_at"],
            },
        }
        for name, role in roles.items()
    }
    agents: dict[str, dict[str, Any]] = {
        name: {
            "mode": "subagent",
            "model": f"af/{role}",
            "description": description,
            "permission": {"task": "deny"},
        }
        for name, (role, description) in AGENTS.items()
    }
    agents["orchestrator"] = {
        "mode": "primary",
        "model": "af/orchestrator",
        "description": "Runs the analysis through its subagents.",
        "permission": {"task": {"*": "deny", **dict.fromkeys(AGENTS, "allow")}},
    }
    agents["compaction"] = {"model": "af/compaction"}
    for builtin in ("build", "plan", "general", "explore", "title"):
        agents[builtin] = {"disable": True}
    return {
        "model": "af/orchestrator",
        "default_agent": "orchestrator",
        "enabled_providers": ["af"],
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
        "subagent_depth": 1,
        # OpenCode would load only the first of AGENTS.md and CLAUDE.md it finds.
        "instructions": [str(facility), str(work / "CLAUDE.md")],
        "compaction": {"auto": True, "prune": True},
        "provider": {
            "af": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "AF",
                "options": {
                    "baseURL": f"{os.environ[PROXY]}/v1",
                    "apiKey": f"{{env:{TOKEN}}}",
                    **WAITS,
                },
                "models": models,
            }
        },
        "agent": agents,
    }


def _image_bytes(role: dict[str, Any]) -> int:
    """The largest image a session of `role` may read: as many as it takes, in
    base64, beside a full context's text, stay within the proxy's body cap."""
    images: int = role["images"]
    if not images:
        return 0
    room: int = role["max_body_bytes"] - role["context"] * TOKEN_BYTES
    return room * 3 // 4 // images


def _setup(
    local: Path, roles: dict[str, dict[str, Any]], work: Path, home: Path
) -> dict[str, str]:
    """Writes OpenCode's configuration directory into `local`, with the plugin
    and FACILITY, read-only once written. Returns the environment of a turn."""
    state = home / "opencode"
    config = local / "config" / "opencode"
    (config / "plugin").mkdir(parents=True)
    plugin_file = config / "plugin" / PLUGIN.name
    shutil.copy(PLUGIN, plugin_file)
    # As the global AGENTS.md, which OpenCode loads instead of a ~/.claude/CLAUDE.md.
    facility = config / "AGENTS.md"
    facility.write_text(
        FACILITY.format(
            cores=CORES,
            hours=COMMAND_HOURS,
            minutes=COMMAND_MINUTES,
            python=PYTHON,
            root=ROOT,
        )
    )
    for file in (plugin_file, facility):
        file.chmod(0o444)
    # OpenCode installs npm packages into a configuration directory it can write.
    for directory in (config / "plugin", config, config.parent):
        directory.chmod(0o555)
    role_of = {"orchestrator": "orchestrator"} | {n: r for n, (r, _) in AGENTS.items()}
    plugin = {
        "records": str(state / "records.jsonl"),
        "helpers": str(_helpers(local)),
        "hook": HOOK,
        "tasks": str(state / "tasks"),
        "seats": SEATS,
        "images": {name: roles[role]["images"] for name, role in role_of.items()},
        "image_bytes": {
            name: _image_bytes(roles[role]) for name, role in role_of.items()
        },
        "timeout": COMMAND_HOURS * 3_600_000,
        # The pod's own, for the agents' commands.
        "env": {
            name: os.environ.get(name, str(Path.home() / default))
            for name, default in XDG.items()
        },
    }
    return {
        **os.environ,
        "OPENCODE_CONFIG_CONTENT": json.dumps(_config(roles, facility, work)),
        "OPENCODE_PERMISSION": json.dumps(PERMISSION),
        "XDG_CONFIG_HOME": str(config.parent),
        "OPENCODE_CONFIG_DIR": str(config),
        "XDG_CACHE_HOME": str(local / "cache"),
        "XDG_STATE_HOME": str(local / "state"),
        # The sessions, which a later run resumes.
        "XDG_DATA_HOME": str(state / "data"),
        "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS": str(COMMAND_MINUTES * 60_000),
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "AF_PLUGIN": json.dumps(plugin),
        # Tags of the profiles the analysis's scripts send.
        "AF_ANALYSIS": work.name,
        "AF_WORKSPACE": home.name,
    }


def _quarantine(work: Path, home: Path) -> None:
    """Moves CONFIGS out of the analysis and the directories above it in the
    workspace, and the .opencode OpenCode loads from $HOME, into the
    workspace's opencode/quarantine."""
    found = [
        home / up / name for up in work.relative_to(home).parents for name in CONFIGS
    ]
    for top, directories, files in os.walk(work):
        found += [
            Path(top) / name for name in (*directories, *files) if name in CONFIGS
        ]
        directories[:] = [
            d for d in directories if d not in (".git", ".pixi", *CONFIGS)
        ]
    aside = home / "opencode" / "quarantine" / f"{time.time():.0f}"
    moves = [(path, aside / path.relative_to(home)) for path in found]
    moves.append((Path.home() / ".opencode", aside / "home" / ".opencode"))
    for path, moved in moves:
        if path.is_symlink() or path.exists():
            moved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(path, moved)
            _log(f"moved {path} to {moved}")


def _turn(work: Path, home: Path, reply: str) -> tuple[list[str], str]:
    """The OpenCode command of the next turn and its prompt: the analysis's
    prompt on a new session; on the one that exists, the human's reply, or a
    nudge without one."""
    command = ["opencode", "run", "--format", "json", "--dir", str(work)]
    command += ["--agent", "orchestrator"]
    session = home / "opencode" / "session"
    if session.exists():
        prompt = REPLY.format(reply=reply) if reply else INTERRUPTED
        return [*command, "--session", session.read_text().strip()], prompt
    return command, UNATTENDED + (work / "prompt.md").read_text(encoding="utf-8")


def _narrate(event: dict[str, Any]) -> str:
    """An event of the main session, or a record of a subagent's, as one log
    line; empty for those not worth one."""
    part = event.get("part") or {}
    state = part.get("state") or {}
    error = event.get("error") or {}
    match event.get("type") or event.get("kind"):
        # A call OpenCode prunes the output of is published again.
        case "tool_use" if not (state.get("time") or {}).get("compacted"):
            line = f"{part.get('tool')} {json.dumps(state.get('input'))}"
            if state.get("status") == "error":
                line += f" failed: {state.get('error')}"
        case "text" if not part.get("synthetic"):
            line = part.get("text") or ""
        case "step_finish" if part.get("reason") == "length":
            line = "the reply ran out of output tokens"
        case "cut":
            line = f"{event.get('agent')}: the reply ran out of output tokens"
        case "error" if "kind" in event:
            line = f"{event.get('agent')} {event.get('error')}: {event.get('message')}"
        case "error":
            line = f"{error.get('name')}: {(error.get('data') or {}).get('message')}"
        case "tool":
            line = f"{event.get('agent')} {event.get('tool')} {event.get('input')}"
            if event.get("status") == "error":
                line += f" failed: {event.get('error')}"
        case _:
            return ""
    return " ".join(line.split())[:LOG_LINE]


class _Watch:
    """Follows a turn's OpenCode process and its plugin's records, from
    `offset` on: ends the turn if the plugin has not loaded within LOADED
    seconds, or if neither has said anything for STALL seconds."""

    def __init__(
        self, process: subprocess.Popen[str], records: Path, turn: str
    ) -> None:
        self.process = process
        self.records = records
        self.turn = turn
        self.offset = records.stat().st_size if records.exists() else 0
        self.loaded = False
        self.started = self.heard = time.monotonic()
        self.ended = ""
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def read(self) -> None:
        """Takes in the records written since the last read, whole lines only."""
        try:
            with open(self.records, "rb") as file:
                file.seek(self.offset)
                written = file.read()
        except FileNotFoundError:
            return
        whole = written[: written.rfind(b"\n") + 1]
        self.offset += len(whole)
        for line in whole.splitlines():
            self.heard = time.monotonic()
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("kind") == "loaded":
                self.loaded = True
            elif record.get("kind") == "helpers":
                failed = record.get("error")
                _log(f"helpers in {record.get('packages')}: {failed or 'put there'}")
            # The main session's events come on stdout.
            elif record.get("parent") and (said := _narrate(record)):
                _log(f"  {said}")

    def _watch(self) -> None:
        while not self.done.wait(POLL):
            self.read()
            if self.process.poll() is not None:
                # What it left behind may hold its output open.
                _kill(self.process, self.turn)
            now = time.monotonic()
            if not self.loaded and now - self.started > LOADED:
                self.end(f"its plugin did not load within {LOADED} s")
            elif now - self.heard > STALL:
                self.end(f"it said nothing for {STALL} s")

    def end(self, why: str) -> None:
        self.ended = why
        _log(f"ending opencode: {why}")
        _signal(self.process, signal.SIGTERM)
        try:
            self.process.wait(GRACE)
        except subprocess.TimeoutExpired:
            pass
        _kill(self.process, self.turn)


def _signal(process: subprocess.Popen[str], number: int) -> None:
    """Signals the process group the turn's OpenCode leads."""
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass


def _kill(process: subprocess.Popen[str], turn: str) -> None:
    """Kills what is left of a turn: OpenCode's process group, and every
    process whose environment carries the turn's MARK, as the commands its bash
    tool starts in sessions of their own do."""
    _signal(process, signal.SIGKILL)
    mark = f"{MARK}={turn}".encode()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if mark in (proc / "environ").read_bytes().split(b"\0"):
                os.kill(int(proc.name), signal.SIGKILL)
        except OSError:
            pass


def _opencode(
    command: list[str],
    prompt: str,
    work: Path,
    env: dict[str, str],
    home: Path,
    taken: Callable[[], object] | None = None,
) -> tuple[list[str], str, str]:
    """Runs one turn to its end, narrating it, and calls `taken` once OpenCode
    holds the prompt. Returns each reply OpenCode compacted the session after
    and had it go on from, the agent's last message, and why its last step
    ended."""
    session = home / "opencode" / "session"
    known = session.exists()
    said, finish, steps = "", "", 0
    text: list[str] = []
    replies: list[str] = []
    # The agent's last message as each of the last two steps since the last compaction ended, and why it ended.
    ended: list[tuple[str, str]] = []
    error: dict[str, Any] = {}
    # The step that compacted the session after a context overflow.
    compacted = 0
    turn = uuid.uuid4().hex
    with subprocess.Popen(
        command,
        cwd=work,
        env={**env, MARK: turn},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        watch = _Watch(process, home / "opencode" / "records.jsonl", turn)
        try:
            assert process.stdin and process.stdout
            # On stdin: OpenCode quotes a prompt given as an argument.
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except BrokenPipeError:
                pass
            for line in process.stdout:
                watch.heard = time.monotonic()
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                if not isinstance(event, dict):
                    _log(line.rstrip())
                    continue
                if event.get("sessionID"):
                    if not known:
                        session.write_text(event["sessionID"])
                        known = True
                    if taken:
                        taken()
                        taken = None
                part = event.get("part") or {}
                if event.get("type") == "step_start":
                    text = []
                elif event.get("type") == "text" and not part.get("synthetic"):
                    text.append(part.get("text") or "")
                    said = "\n\n".join(text)
                elif (part.get("metadata") or {}).get("compaction_continue"):
                    # OpenCode compacted the session after the step before the summary, and prompts it to go on.
                    if len(ended) == 2 and ended[0][1] not in ONGOING and ended[0][0]:
                        replies.append(ended[0][0])
                    said, ended = "", []
                elif event.get("type") == "step_finish":
                    finish, steps = part.get("reason") or "", steps + 1
                    # OpenCode compacts after a context overflow, this step its summary, and goes on.
                    if error.get("name") == "ContextOverflowError":
                        error, compacted, said = {}, steps, ""
                    ended = [*ended[-1:], (said, finish)]
                elif event.get("type") == "error":
                    error = event.get("error") or {}
                if narrated := _narrate(event):
                    _log(narrated)
            process.wait()
        finally:
            watch.done.set()
            watch.thread.join()
            watch.read()
            _kill(process, turn)
    if watch.ended:
        raise RuntimeError(f"opencode: {watch.ended}")
    if not watch.loaded:
        raise RuntimeError("opencode: its plugin did not load")
    if error:
        detail = (error.get("data") or {}).get("message") or error.get("name")
        if (error.get("data") or {}).get("statusCode") in (401, 403):
            raise flyte.errors.NonRecoverableError(f"opencode: {detail}")
        raise RuntimeError(f"opencode: {detail}")
    # A context overflow leaves OpenCode's exit status at 1 though it compacted and went on to the turn's end.
    went_on = 0 < compacted < steps and finish not in ONGOING
    if process.returncode and not (process.returncode == 1 and went_on):
        raise RuntimeError(f"opencode exited {process.returncode}")
    _log(f"turn over: {steps} steps, the last ending in {finish or 'nothing'}")
    return replies, said, finish


def _agent(work: Path, home: Path, reply: str, told: Path) -> str:
    """The agent's work until it ends its turn: each turn on OpenCode
    configured afresh from the proxy's roles, a turn resumed while its last
    reply ran out of output tokens. The reply is written to `told` once
    OpenCode holds it. Returns what the agent said."""
    (home / "opencode").mkdir(exist_ok=True)
    roles = _roles()
    kept: list[str] = []
    for _ in range(RESUMES + 1):
        _quarantine(work, home)
        command, prompt = _turn(work, home, reply)
        taken = functools.partial(told.write_text, reply) if reply else None
        # Nothing a turn writes into it reaches the next.
        with tempfile.TemporaryDirectory(
            prefix="opencode-", ignore_cleanup_errors=True
        ) as local:
            env = _setup(Path(local), roles, work, home)
            replies, said, finish = _opencode(command, prompt, work, env, home, taken)
        kept += replies
        if finish != "length":
            return "\n\n".join(filter(None, [*kept, said]))
        reply = ""
    raise RuntimeError(
        f"the agent's replies ran out of output tokens {RESUMES + 1} times"
    )


# A run lasts until the agent ends its turn, which can take an analysis days.
@env.task(retries=RETRIES, timeout=timedelta(days=14))
def advance(analysis: Analysis, workspace: str, reply: str) -> str:
    """One stretch of the agent's work in ROOT/<workspace>, until it ends its
    turn: with the analysis note finished, or with something for a person.
    Returns what it said."""
    if not (os.environ.get(PROXY) and os.environ.get(TOKEN)):
        raise flyte.errors.NonRecoverableError(
            f"{PROXY} and {TOKEN} are not both set: see apps/agentic-analysis/README.md"
        )
    if not Path(os.environ.get(af_dask.TOKEN_FILE, "")).is_file():
        raise flyte.errors.NonRecoverableError(
            f"{af_dask.TOKEN_FILE} names no file of the workload account's token: see apps/agentic-analysis/README.md"
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
            # A reply reaches the agent once: a retry of the run whose turn took it nudges.
            told = home / "replies" / ctx.action.run_name
            told.parent.mkdir(exist_ok=True)
            said = _agent(work, home, "" if told.exists() else reply, told)
            (home / "said.md").write_text(said)
    except flyte.errors.NonRecoverableError:
        raise
    except RuntimeError as failed:
        # The backend restarts a failed attempt at once; a service that failed may need longer.
        if ctx.attempt_number < RETRIES:
            pause = min(15 * 2**ctx.attempt_number, 240)
            _log(f"{failed}; next attempt in {pause} min")
            time.sleep(pause * 60)
        raise
    print(said, flush=True)
    return said


def _sources(directory: Path, names: list[str]) -> dict[str, str]:
    """prompt.md and the files `names` name in `directory`, by relative path."""
    found = {}
    for name in ("prompt.md", *names):
        path = directory / name
        if not path.exists():
            raise FileNotFoundError(f"{path}, which analysis.toml names")
        for file in sorted(path.rglob("*")) if path.is_dir() else [path]:
            hidden = any(
                part.startswith(".") or part == "__pycache__"
                for part in file.relative_to(directory).parts
            )
            if file.is_file() and not hidden:
                found[str(file.relative_to(directory))] = file.read_text(
                    encoding="utf-8"
                )
    return found


def launch(directory: str, workspace: str = "", *reply: str) -> None:
    """Starts a run of the analysis in `directory` (README.md, Running)."""
    from flyte.remote import Project

    with open(Path(directory) / "analysis.toml", "rb") as file:
        analysis = Analysis(**tomllib.load(file))
    if not analysis.repo:
        analysis.sources = _sources(Path(directory), analysis.files)
    flyte.init_from_config("config.yaml", root_dir=Path(__file__).parent)
    if NAME not in {project.pb2.id for project in Project.listall()}:
        Project.create(id=NAME, name=NAME)
    name = f"{analysis.name}-{datetime.now(timezone.utc):%m%d-%H%M%S}"
    run = flyte.with_runcontext(name=name).run(
        advance, analysis=analysis, workspace=workspace or name, reply=" ".join(reply)
    )
    print(run.name, run.url)


if __name__ == "__main__":
    launch(*sys.argv[1:])
