"""workflows/jfc-h4l against a stub `flyte` (the SDK is not a test dependency),
and its wiring to apps/jfc-h4l. Git, pixi, the download and the agent are
replaced per test."""

import fcntl
import io
import json
import sys
import tarfile
import types

import pytest
import yaml
from common import REPO, load_script

APP = REPO / "apps" / "jfc-h4l"
WORKFLOW = REPO / "workflows" / "jfc-h4l"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"


def _load():
    flyte = types.ModuleType("flyte")
    flyte.TaskEnvironment = lambda **options: types.SimpleNamespace(
        options=options, task=lambda **task: lambda fn: fn
    )
    flyte.Resources = lambda **kw: kw
    flyte.errors = types.ModuleType("flyte.errors")
    flyte.errors.NonRecoverableError = type("NonRecoverable", (RuntimeError,), {})
    stubs = {"flyte": flyte, "flyte.errors": flyte.errors}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        return load_script(WORKFLOW / "jfc_h4l.py", "jfc_h4l_flows")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


jfc = _load()


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(jfc, "ROOT", tmp_path)
    return tmp_path


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


# --- wiring ---


def test_flux_lists_every_manifest_of_the_app():
    resources = yaml.safe_load(EXPERIMENTAL.read_text())["resources"]
    assert {r for r in resources if "jfc-h4l" in r} == {
        f"../../apps/jfc-h4l/{path.name}" for path in APP.glob("*.yaml")
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


# --- setup ---


def _fake_tools(monkeypatch, data):
    """git and pixi as recorded calls; the scaffolder as the directory it makes."""
    calls = []

    def checkout(source, dest):
        calls.append(("checkout", dest.name))
        (dest / "docs").mkdir(parents=True)
        (dest / "docs" / "paper.pdf").write_text("paper")
        (dest / "h4l_ntuplize.py").write_text("ntuplizer")
        (dest / "prompt.md").write_text("measure the Higgs mass")

    def run(*command, cwd):
        calls.append(command[:2])
        if "src/scaffold_analysis.py" in command:
            (cwd / command[2]).mkdir(parents=True)

    monkeypatch.setattr(jfc, "_checkout", checkout)
    monkeypatch.setattr(jfc, "_run", run)
    monkeypatch.setattr(jfc, "_data", lambda: data)
    return calls


def test_prepare_stages_the_exercise_into_a_scaffolded_analysis(root, monkeypatch):
    data = root / "data"
    data.mkdir()
    calls = _fake_tools(monkeypatch, data)

    analysis = jfc._prepare(root / "run")

    assert analysis == root / "run/jfc/analyses/h4l"
    assert (analysis / "data").resolve() == data.resolve()
    assert (analysis / "prompt.md").read_text() == "measure the Higgs mass"
    assert (analysis / "docs/paper.pdf").is_file()
    assert (analysis / "h4l_ntuplize.py").is_file()
    assert calls[-1] == ("pixi", "install")


def test_prepare_leaves_a_staged_workspace_to_the_agent(root, monkeypatch):
    calls = _fake_tools(monkeypatch, root)
    analysis = jfc._prepare(root / "run")
    (analysis / "prompt.md").unlink()
    (analysis / "experiment_log.md").write_text("the agent's work")
    calls.clear()

    assert jfc._prepare(root / "run") == analysis

    assert calls == []
    assert (analysis / "experiment_log.md").is_file()


def test_prepare_redoes_a_setup_that_died_before_its_last_step(root, monkeypatch):
    calls = _fake_tools(monkeypatch, root)
    run = jfc._run

    def dies_installing(*command, cwd):
        run(*command, cwd=cwd)
        if command[0] == "pixi":
            raise OSError("killed")

    monkeypatch.setattr(jfc, "_run", dies_installing)
    with pytest.raises(OSError):
        jfc._prepare(root / "run")
    monkeypatch.setattr(jfc, "_run", run)
    calls.clear()

    jfc._prepare(root / "run")

    assert calls[:2] == [("checkout", "jfc"), ("checkout", "h4l_agent_test")]
    assert calls[-1] == ("pixi", "install")


def test_data_is_fetched_once(root, tmp_path_factory, monkeypatch):
    source = tmp_path_factory.mktemp("s3") / "h4l.tgz"
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

    assert jfc._data() == jfc._data() == root / "data"
    assert (root / "data/ZZTo4L.root").read_bytes() == b"events"
    assert fetched == [jfc.DATA]
    assert [path.name for path in root.iterdir()] == ["data"]


# --- the agent ---


def _workspace(root):
    analysis, config = root / "analysis", root / "claude"
    analysis.mkdir()
    (analysis / "prompt.md").write_text("measure the Higgs mass")
    return analysis, config


def _session(command):
    flag = "--resume" if "--resume" in command else "--session-id"
    return flag, command[command.index(flag) + 1]


def test_first_turn_opens_a_session_with_the_exercise_prompt(root):
    analysis, config = _workspace(root)

    command = jfc._turn(analysis, config, "")

    assert command[:2] == ["claude", "--print"]
    assert command[2].endswith("measure the Higgs mass")
    assert _session(command)[0] == "--session-id"
    assert command[command.index("--permission-mode") + 1] == jfc.PERMISSION_MODE
    assert command[command.index("--model") + 1] == jfc.MODEL


def test_later_turns_resume_that_session_with_the_reply_or_a_nudge(root):
    analysis, config = _workspace(root)
    _, session = _session(jfc._turn(analysis, config, ""))
    (config / "projects/slug").mkdir(parents=True)
    (config / "projects/slug" / f"{session}.jsonl").write_text("{}")

    replied = jfc._turn(analysis, config, "APPROVE")
    nudged = jfc._turn(analysis, config, "")

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
    analysis, _ = _workspace(root)
    state = types.SimpleNamespace(
        attempt_number=0,
        action=types.SimpleNamespace(run_name="run-1"),
        prompts=[],
        slept=[],
    )

    def claude(command, cwd, config):
        state.prompts.append(command[2])
        (config / "projects/slug").mkdir(parents=True, exist_ok=True)
        (config / "projects/slug" / f"{_session(command)[1]}.jsonl").write_text("{}")
        return "At the human gate."

    monkeypatch.setattr(jfc.flyte, "ctx", lambda: state, raising=False)
    monkeypatch.setattr(jfc, "_prepare", lambda home: analysis)
    monkeypatch.setattr(jfc, "_claude", claude)
    monkeypatch.setattr(jfc.time, "sleep", state.slept.append)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stand-in")
    return state


def test_advance_refuses_to_start_without_credentials(root, monkeypatch):
    for name in jfc.CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(jfc, "_prepare", lambda home: pytest.fail("set up anyway"))

    with pytest.raises(jfc.flyte.errors.NonRecoverableError):
        jfc.advance("ws", "")


def test_advance_returns_and_keeps_what_the_agent_said(root, stretch):
    assert jfc.advance("ws", "") == "At the human gate."
    assert (root / "ws/said.md").read_text() == "At the human gate."
    assert stretch.slept == []


def test_advance_hands_a_reply_to_the_agent_once(root, stretch):
    jfc.advance("ws", "")
    stretch.action.run_name = "run-2"

    jfc.advance("ws", "REGRESS(2)")
    jfc.advance("ws", "REGRESS(2)")  # run-2 retried

    assert stretch.prompts[1:] == [
        jfc.REPLY.format(reply="REGRESS(2)"),
        jfc.INTERRUPTED,
    ]
    assert (root / "ws/replies/run-2").read_text() == "REGRESS(2)"


def test_advance_refuses_a_reply_to_a_workspace_that_does_not_exist(root, stretch):
    with pytest.raises(jfc.flyte.errors.NonRecoverableError):
        jfc.advance("typo", "APPROVE")
    assert stretch.prompts == [] and not (root / "typo").exists()


def test_advance_runs_the_agent_under_the_workspace_lock(root, stretch, monkeypatch):
    def claude(command, cwd, config):
        with open(root / "ws/lock") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return ""

    monkeypatch.setattr(jfc, "_claude", claude)
    jfc.advance("ws", "")


def _failing(error):
    def claude(command, cwd, config):
        raise error

    return claude


def test_advance_waits_after_a_failed_turn_longer_each_time(stretch, monkeypatch):
    monkeypatch.setattr(jfc, "_claude", _failing(RuntimeError("usage limit")))

    for stretch.attempt_number in (0, 1, 4, jfc.RETRIES):
        with pytest.raises(RuntimeError, match="usage limit"):
            jfc.advance("ws", "")

    assert stretch.slept == [15 * 60, 30 * 60, 240 * 60]


def test_advance_does_not_wait_on_a_failure_no_retry_follows(stretch, monkeypatch):
    final = jfc.flyte.errors.NonRecoverableError("claude: Invalid API key")
    monkeypatch.setattr(jfc, "_claude", _failing(final))

    with pytest.raises(jfc.flyte.errors.NonRecoverableError):
        jfc.advance("ws", "")

    assert stretch.slept == []
