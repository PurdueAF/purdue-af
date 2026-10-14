"""workflows/jfc against a stub `flyte` (the SDK is not a test dependency), its
wiring to apps/jfc, and the analyses beside it. Git, pixi, the download and the
agent are replaced per test."""

import fcntl
import io
import json
import re
import sys
import tarfile
import types

import pytest
import yaml
from common import REPO, load_script

APP = REPO / "apps" / "jfc"
WORKFLOW = REPO / "workflows" / "jfc"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"


class Launcher:
    """What the launcher asks of the control plane, recorded."""

    projects: list[str] = []
    runs: list[dict] = []

    @classmethod
    def listall(cls):
        return [
            types.SimpleNamespace(pb2=types.SimpleNamespace(id=p)) for p in cls.projects
        ]

    @classmethod
    def create(cls, id, name):
        cls.projects.append(id)

    @classmethod
    def with_runcontext(cls, name):
        def run(task, **inputs):
            cls.runs.append({"name": name, "task": task, **inputs})
            return types.SimpleNamespace(name=name, url="url")

        return types.SimpleNamespace(run=run)


def _load():
    flyte = types.ModuleType("flyte")
    flyte.TaskEnvironment = lambda **options: types.SimpleNamespace(
        options=options, task=lambda **task: lambda fn: fn
    )
    flyte.Resources = lambda **kw: kw
    flyte.init_from_config = lambda *args, **kw: None
    flyte.with_runcontext = Launcher.with_runcontext
    flyte.errors = types.ModuleType("flyte.errors")
    flyte.errors.NonRecoverableError = type("NonRecoverable", (RuntimeError,), {})
    flyte.remote = types.ModuleType("flyte.remote")
    flyte.remote.Project = Launcher
    stubs = {"flyte": flyte, "flyte.errors": flyte.errors, "flyte.remote": flyte.remote}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        return load_script(WORKFLOW / "jfc.py", "jfc_flows"), stubs
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


jfc, STUBS = _load()

ANALYSIS = jfc.Analysis(
    name="zz",
    kind="search",
    repo="https://example.org/zz.git",
    ref="c0ffee",
    files=["docs", "ntuplize.py"],
    data="https://example.org/zz.tgz",
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(jfc, "ROOT", tmp_path)
    return tmp_path


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


# --- wiring ---


def test_flux_lists_every_manifest_of_the_app():
    resources = yaml.safe_load(EXPERIMENTAL.read_text())["resources"]
    assert {r for r in resources if "/apps/jfc/" in r} == {
        f"../../apps/jfc/{path.name}" for path in APP.glob("*.yaml")
    }


def test_the_task_runs_in_the_pod_template_and_project_named_after_the_workflow():
    (template,) = docs(APP / "podtemplate.yaml")
    config = yaml.safe_load((WORKFLOW / "config.yaml").read_text())
    flyte_values = yaml.safe_load((REPO / "apps/flyte/values.yaml").read_text())
    assert jfc.env.options["pod_template"] == template["metadata"]["name"]
    assert config["task"]["project"] == jfc.NAME
    assert f"{flyte_values['fullnameOverride']}-http." in config["admin"]["endpoint"]


def test_task_pods_mount_only_the_directory_the_workspace_job_creates_for_them():
    (template,) = docs(APP / "podtemplate.yaml")
    (job,) = docs(APP / "workspace.yaml")
    pod = template["template"]["spec"]
    (mount,) = pod["containers"][0]["volumeMounts"]
    assert mount["mountPath"] == str(jfc.ROOT) == f"/work/{mount['subPath']}"

    container = job["spec"]["template"]["spec"]["containers"][0]
    (made,) = container["volumeMounts"]
    directory = f"{made['mountPath']}/{jfc.NAME}"
    owner = "{runAsUser}:{runAsGroup}".format(**pod["securityContext"])
    assert f"{made['subPath']}/{jfc.NAME}" == mount["subPath"]
    assert container["command"][-1] == (
        f"mkdir -p {directory} && chown {owner} {directory}"
    )


def test_the_credentials_secret_is_optional_so_a_task_can_say_it_is_missing():
    (template,) = docs(APP / "podtemplate.yaml")
    (source,) = template["template"]["spec"]["containers"][0]["envFrom"]
    assert source["secretRef"]["optional"] is True


# --- the launcher ---


@pytest.fixture
def launched(tmp_path, monkeypatch):
    for name, module in STUBS.items():
        monkeypatch.setitem(sys.modules, name, module)
    (tmp_path / "analysis.toml").write_text(
        'name = "zz"\nkind = "search"\nrepo = "https://example.org/zz.git"\nref = "c0ffee"\n'
    )
    Launcher.projects[:] = []
    Launcher.runs[:] = []
    return str(tmp_path)


def test_every_analysis_directory_launches_a_run_pinned_to_a_commit(launched):
    described = sorted((REPO / "workflows").glob("jfc-*/analysis.toml"))
    assert described
    for path in described:
        jfc.launch(str(path.parent))
        run = Launcher.runs[-1]
        analysis = run["analysis"]
        assert path.parent.name == f"jfc-{analysis.name}"
        assert analysis.kind in ("measurement", "search")
        assert re.fullmatch(r"[0-9a-f]{40}", analysis.ref)
        assert len(run["name"]) <= 30


def test_launch_starts_a_run_named_after_the_analysis_in_a_workspace_of_its_own(
    launched,
):
    jfc.launch(launched)
    jfc.launch(launched)

    first, _ = Launcher.runs
    assert first["task"] is jfc.advance
    assert first["analysis"] == jfc.Analysis(
        "zz", "search", "https://example.org/zz.git", "c0ffee"
    )
    assert first["name"].startswith("jfc-zz-") and first["workspace"] == first["name"]
    assert first["reply"] == ""
    assert Launcher.projects == [jfc.NAME]


def test_launch_carries_a_reply_whole_to_the_workspace_it_names(launched):
    jfc.launch(launched, "jfc-zz-1013-120000", "ITERATE:", "redo the fit")

    (run,) = Launcher.runs
    assert run["workspace"] == "jfc-zz-1013-120000"
    assert run["reply"] == "ITERATE: redo the fit"


# --- setup ---


def _fake_tools(monkeypatch, data):
    """git and pixi as recorded calls; the scaffolder as the directory it makes."""
    calls = []

    def checkout(source, dest):
        calls.append(("checkout", dest.name, *source))
        (dest / "docs").mkdir(parents=True)
        (dest / "docs" / "paper.pdf").write_text("paper")
        (dest / "ntuplize.py").write_text("ntuplizer")
        (dest / "prompt.md").write_text("search for ZZ")

    def run(*command, cwd):
        calls.append(command[1:] if command[0] == sys.executable else command[:2])
        if "src/scaffold_analysis.py" in command:
            (cwd / command[2]).mkdir(parents=True)

    monkeypatch.setattr(jfc, "_checkout", checkout)
    monkeypatch.setattr(jfc, "_run", run)
    monkeypatch.setattr(jfc, "_data", lambda analysis: data)
    return calls


def test_prepare_stages_the_analysis_into_its_scaffold(root, monkeypatch):
    data = root / "data"
    data.mkdir()
    calls = _fake_tools(monkeypatch, data)

    work = jfc._prepare(root / "run", ANALYSIS)

    assert work == root / "run/jfc/analyses/zz"
    assert ("checkout", "jfc", *jfc.JFC) in calls
    assert ("checkout", "source", ANALYSIS.repo, ANALYSIS.ref) in calls
    assert ("src/scaffold_analysis.py", "analyses/zz", "--type", "search") in calls
    assert (work / "data").resolve() == data.resolve()
    assert (work / "prompt.md").read_text() == "search for ZZ"
    assert (work / "docs/paper.pdf").is_file()
    assert (work / "ntuplize.py").is_file()
    assert calls[-1] == ("pixi", "install")


def test_prepare_stages_only_the_prompt_of_an_analysis_that_names_nothing_else(
    root, monkeypatch
):
    _fake_tools(monkeypatch, root)
    bare = jfc.Analysis("zz", "search", ANALYSIS.repo, ANALYSIS.ref)

    work = jfc._prepare(root / "run", bare)

    assert sorted(path.name for path in work.iterdir()) == ["prompt.md"]


def test_prepare_leaves_a_staged_workspace_to_the_agent(root, monkeypatch):
    calls = _fake_tools(monkeypatch, root)
    work = jfc._prepare(root / "run", ANALYSIS)
    (work / "prompt.md").unlink()
    (work / "experiment_log.md").write_text("the agent's work")
    calls.clear()

    assert jfc._prepare(root / "run", ANALYSIS) == work

    assert calls == []
    assert (work / "experiment_log.md").is_file()


def test_prepare_redoes_a_setup_that_died_before_its_last_step(root, monkeypatch):
    calls = _fake_tools(monkeypatch, root)
    run = jfc._run

    def dies_installing(*command, cwd):
        run(*command, cwd=cwd)
        if command[0] == "pixi":
            raise OSError("killed")

    monkeypatch.setattr(jfc, "_run", dies_installing)
    with pytest.raises(OSError):
        jfc._prepare(root / "run", ANALYSIS)
    monkeypatch.setattr(jfc, "_run", run)
    calls.clear()

    jfc._prepare(root / "run", ANALYSIS)

    assert [call[:2] for call in calls[:2]] == [
        ("checkout", "jfc"),
        ("checkout", "source"),
    ]
    assert calls[-1] == ("pixi", "install")


def test_data_is_fetched_once_per_url(root, tmp_path_factory, monkeypatch):
    source = tmp_path_factory.mktemp("s3") / "zz.tgz"
    with tarfile.open(source, "w:gz") as tar:
        info = tarfile.TarInfo("data/ZZTo4L.root")
        info.size = 6
        tar.addfile(info, io.BytesIO(b"events"))
    fetched = []

    def urlretrieve(url, dest):
        fetched.append(url)
        dest.write_bytes(source.read_bytes())
        return dest, None

    monkeypatch.setattr(jfc.urllib.request, "urlretrieve", urlretrieve)

    data = jfc._data(ANALYSIS)
    assert jfc._data(ANALYSIS) == data and data.parent == root / "data"
    assert (data / "ZZTo4L.root").read_bytes() == b"events"
    assert fetched == [ANALYSIS.data]

    moved = jfc.Analysis(
        "zz", "search", ANALYSIS.repo, ANALYSIS.ref, data="https://example.org/v2.tgz"
    )
    assert jfc._data(moved) != data
    assert sorted(path.name for path in (root / "data").iterdir()) == sorted(
        [data.name, jfc._data(moved).name]
    )


# --- the agent ---


def _workspace(root):
    work, config = root / "jfc" / "analyses" / "zz", root / "claude"
    work.mkdir(parents=True)
    (work / "prompt.md").write_text("search for ZZ")
    return work, config


def _session(command):
    flag = "--resume" if "--resume" in command else "--session-id"
    return flag, command[command.index(flag) + 1]


def test_first_turn_opens_a_session_with_the_analysis_prompt(root):
    work, config = _workspace(root)

    command = jfc._turn(work, config, "")

    assert command[:2] == ["claude", "--print"]
    assert command[2].endswith("search for ZZ")
    assert _session(command)[0] == "--session-id"
    assert command[command.index("--permission-mode") + 1] == jfc.PERMISSION_MODE
    assert command[command.index("--model") + 1] == jfc.MODEL
    # The scaffold's methodology, agents and conventions link into it.
    assert command[command.index("--add-dir") + 1] == str(root / "jfc" / "src")


def test_later_turns_resume_that_session_with_the_reply_or_a_nudge(root):
    work, config = _workspace(root)
    _, session = _session(jfc._turn(work, config, ""))
    (config / "projects/slug").mkdir(parents=True)
    (config / "projects/slug" / f"{session}.jsonl").write_text("{}")

    replied = jfc._turn(work, config, "APPROVE")
    nudged = jfc._turn(work, config, "")

    assert _session(replied) == _session(nudged) == ("--resume", session)
    assert replied[2] == jfc.REPLY.format(reply="APPROVE")
    assert nudged[2] == jfc.INTERRUPTED


def _fake_claude(tmp_path, lines, code=0):
    """A stand-in for the CLI: prints `lines`, then exits with `code`."""
    script = tmp_path / "claude.py"
    script.write_text(
        "import os, sys\n"
        f"for line in {lines!r}:\n"
        "    print(line.replace('$CONFIG', os.environ['CLAUDE_CONFIG_DIR']))\n"
        f"sys.exit({code})\n"
    )
    return [sys.executable, str(script)]


def _assistant(*content, **extra):
    return json.dumps({"type": "assistant", "message": {"content": content}, **extra})


def test_claude_returns_the_result_and_narrates_the_turn(tmp_path, capsys):
    tool = {"type": "tool_use", "name": "Bash", "input": {"command": "pixi run all"}}
    lines = [
        "a warning that is not JSON",
        _assistant(tool),
        _assistant({"type": "text", "text": "fit\nconverged"}, parent_tool_use_id="t"),
        _assistant({"type": "text", "text": "x" * 999}),
        _assistant(parent_tool_use_id="t"),
        json.dumps({"type": "user", "message": {"content": "tool output"}}),
        json.dumps(
            {
                "type": "system",
                "subtype": "api_retry",
                "attempt": 2,
                "max_retries": 10,
                "error": "rate_limit",
            }
        ),
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "gate: $CONFIG",
                "num_turns": 7,
                "total_cost_usd": 1.5,
            }
        ),
    ]

    said = jfc._claude(_fake_claude(tmp_path, lines), tmp_path, tmp_path / "cfg")

    assert said == f"gate: {tmp_path / 'cfg'}"
    logged = [line.split("] ", 1)[1] for line in capsys.readouterr().out.splitlines()]
    assert logged == [
        "a warning that is not JSON",
        'Bash {"command": "pixi run all"}',
        "  fit converged",
        "x" * jfc.LOG_LINE,
        "API retry 2/10: rate_limit",
        "turn over: 7 turns, $1.50",
    ]


@pytest.mark.parametrize(
    "result, code",
    [
        ({"type": "result", "is_error": True, "result": "Not logged in"}, 1),
        ({"type": "result", "is_error": False, "result": "done"}, 143),
        ({"type": "system", "subtype": "init"}, 0),
    ],
)
def test_claude_raises_unless_the_turn_ended_with_a_clean_result(
    tmp_path, result, code
):
    command = _fake_claude(tmp_path, [json.dumps(result)], code)
    with pytest.raises(RuntimeError, match="claude exited"):
        jfc._claude(command, tmp_path, tmp_path / "cfg")


def test_a_rejected_credential_is_not_worth_a_retry(tmp_path):
    result = {"type": "result", "is_error": True, "api_error_status": 401}
    command = _fake_claude(tmp_path, [json.dumps(result)], 1)
    with pytest.raises(jfc.flyte.errors.NonRecoverableError):
        jfc._claude(command, tmp_path, tmp_path / "cfg")


# --- the task ---


@pytest.fixture
def stretch(root, monkeypatch):
    """`advance` with the setup done, the agent recorded, and no real pauses."""
    work, _ = _workspace(root)
    state = types.SimpleNamespace(
        attempt_number=0,
        action=types.SimpleNamespace(run_name="ws"),
        prompts=[],
        slept=[],
    )

    def claude(command, cwd, config):
        state.prompts.append(command[2])
        (config / "projects/slug").mkdir(parents=True, exist_ok=True)
        (config / "projects/slug" / f"{_session(command)[1]}.jsonl").write_text("{}")
        return "At the human gate."

    monkeypatch.setattr(jfc.flyte, "ctx", lambda: state, raising=False)
    monkeypatch.setattr(jfc, "_prepare", lambda home, analysis: work)
    monkeypatch.setattr(jfc, "_claude", claude)
    monkeypatch.setattr(jfc.time, "sleep", state.slept.append)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stand-in")
    return state


def test_advance_refuses_to_start_without_credentials(root, monkeypatch):
    for name in jfc.CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(jfc, "_prepare", lambda *args: pytest.fail("set up anyway"))

    with pytest.raises(jfc.flyte.errors.NonRecoverableError):
        jfc.advance(ANALYSIS, "ws", "")


def test_advance_returns_and_keeps_what_the_agent_said(root, stretch):
    assert jfc.advance(ANALYSIS, "ws", "") == "At the human gate."
    assert (root / "ws/said.md").read_text() == "At the human gate."
    assert stretch.slept == []


def test_advance_hands_a_reply_to_the_agent_once(root, stretch):
    jfc.advance(ANALYSIS, "ws", "")
    stretch.action.run_name = "run-2"

    jfc.advance(ANALYSIS, "ws", "REGRESS(2)")
    jfc.advance(ANALYSIS, "ws", "REGRESS(2)")  # run-2 retried

    assert stretch.prompts[1:] == [
        jfc.REPLY.format(reply="REGRESS(2)"),
        jfc.INTERRUPTED,
    ]
    assert (root / "ws/replies/run-2").read_text() == "REGRESS(2)"


@pytest.mark.parametrize("reply", ["", "APPROVE"])
def test_advance_refuses_a_workspace_that_is_neither_its_own_nor_there(
    root, stretch, reply
):
    with pytest.raises(jfc.flyte.errors.NonRecoverableError):
        jfc.advance(ANALYSIS, "typo", reply)
    assert stretch.prompts == [] and not (root / "typo").exists()


def test_advance_runs_the_agent_under_the_workspace_lock(root, stretch, monkeypatch):
    def claude(command, cwd, config):
        with open(root / "ws/lock") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return ""

    monkeypatch.setattr(jfc, "_claude", claude)
    jfc.advance(ANALYSIS, "ws", "")


def _failing(error):
    def claude(command, cwd, config):
        raise error

    return claude


def test_advance_waits_after_a_failed_turn_longer_each_time(stretch, monkeypatch):
    monkeypatch.setattr(jfc, "_claude", _failing(RuntimeError("usage limit")))

    for stretch.attempt_number in (0, 1, 4, jfc.RETRIES):
        with pytest.raises(RuntimeError, match="usage limit"):
            jfc.advance(ANALYSIS, "ws", "")

    assert stretch.slept == [15 * 60, 30 * 60, 240 * 60]


def test_advance_does_not_wait_on_a_failure_no_retry_follows(stretch, monkeypatch):
    final = jfc.flyte.errors.NonRecoverableError("claude: Invalid API key")
    monkeypatch.setattr(jfc, "_claude", _failing(final))

    with pytest.raises(jfc.flyte.errors.NonRecoverableError):
        jfc.advance(ANALYSIS, "ws", "")

    assert stretch.slept == []
