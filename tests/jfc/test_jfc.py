"""workflows/jfc against a stub `flyte` (the SDK is not a test dependency), its
wiring to apps/jfc, and the analyses beside it. Git, pixi, the download and the
agent are replaced per test."""

import fcntl
import functools
import io
import json
import re
import sys
import tarfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def test_the_model_key_comes_from_a_secret_the_repository_ships():
    (template,) = docs(APP / "podtemplate.yaml")
    (variable,) = template["template"]["spec"]["containers"][0]["env"]
    wanted = variable["valueFrom"]["secretKeyRef"]
    shipped = [
        doc
        for path in (REPO / "apps").glob("*/secret*.yaml")
        for doc in docs(path)
        if doc["kind"] == "Secret" and doc["metadata"]["name"] == wanted["name"]
    ]
    assert variable["name"] == "ANTHROPIC_AUTH_TOKEN"
    assert [wanted["key"] in doc["stringData"] for doc in shipped] == [True]


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
    work, config = root / "analysis", root / "claude"
    work.mkdir()
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
    assert command[command.index("--allowedTools") + 1] == jfc.ALLOWED_TOOLS
    assert command[command.index("--model") + 1] == jfc.MODEL
    assert command[command.index("--effort") + 1] == jfc.EFFORT


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


ENVIRONMENT = (
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
)


def _fake_claude(tmp_path, lines, code=0):
    """A stand-in for the CLI: prints `lines`, then exits with `code`."""
    script = tmp_path / "claude.py"
    script.write_text(
        "import os, sys\n"
        f"for line in {lines!r}:\n"
        f"    for name in {ENVIRONMENT!r}:\n"
        "        line = line.replace('$' + name, os.environ[name])\n"
        "    print(line)\n"
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
                "result": "gate: " + " ".join(f"${name}" for name in ENVIRONMENT),
                "num_turns": 7,
                "total_cost_usd": 1.5,
            }
        ),
    ]

    said = jfc._claude(_fake_claude(tmp_path, lines), tmp_path, tmp_path / "cfg")

    config, genai, model, reply_room = said.removeprefix("gate: ").split()
    assert config == str(tmp_path / "cfg") and model == jfc.MODEL
    assert int(reply_room) == jfc.OUTPUT < jfc.CONTEXT
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/api", genai)
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


def _image(name):
    return {"type": "image", "source": {"type": "base64", "data": name}}


def _text(text):
    return {"type": "text", "text": text}


def _result(*content, of="t"):
    return {"type": "tool_result", "tool_use_id": of, "content": list(content)}


def _handler():
    return jfc.Messages.__new__(jfc.Messages)


def _reading(*results, max_tokens=1000):
    """A request one step in: the model read a.png (call t) and notes.md (n)
    and ran a command (u), and `results` came back."""
    said = [
        {"type": "thinking", "thinking": "both figures", "signature": ""},
        _text("Reading both."),
        {
            "type": "tool_use",
            "id": "t",
            "name": "Read",
            "input": {"file_path": "a.png"},
        },
        {
            "type": "tool_use",
            "id": "n",
            "name": "Read",
            "input": {"file_path": "notes.md"},
        },
        {"type": "tool_use", "id": "u", "name": "Bash", "input": {"command": "plot"}},
    ]
    return {
        "model": "m",
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": "look at both"},
            {"role": "assistant", "content": said},
            {"role": "user", "content": list(results)},
        ],
    }


def _asked(request, handler=None):
    return json.loads((handler or _handler()).ask(json.dumps(request).encode()))


def _step(request):
    return _asked(request)["messages"][-1]["content"]


def test_tool_results_go_in_the_order_of_their_calls():
    reminder = _text("a reminder")
    late, early, stray = (_result(_text(of), of=of) for of in ("u", "t", "gone"))
    request = _reading(late, reminder, stray, early)
    request["messages"].insert(2, {"role": "system", "content": [_text("a notice")]})
    request["messages"] += [
        {"role": "assistant", "content": [_text("Again.")]},
        {"role": "user", "content": [late, early]},
    ]

    sent = _asked(request)["messages"]

    # Each result stays in a place that held one; one no call asked for goes last.
    assert sent[3]["content"] == [early, reminder, late, stray]
    assert sent[5]["content"] == [late, early]


def test_images_of_tool_results_follow_the_results_of_their_message():
    caption, reminder = _text("a caption"), _text("a reminder")
    drawn = _result(caption, _image("b"), of="u")
    request = _reading(_result(_image("a")), drawn, reminder)

    sent = _asked(request)

    assert sent["messages"][:2] == request["messages"][:2]
    first, second, kept, *moved = sent["messages"][2]["content"]
    assert kept == reminder and second["content"][0] == caption
    assert moved[1::2] == [_image("a"), _image("b")]
    # Each result says which image left it, by the name that image goes under.
    names = [label["text"].removesuffix(":") for label in moved[0::2]]
    for result, name in zip((first, second), names):
        assert name in result["content"][-1]["text"]
    assert "a.png" in names[0] and "a.png" not in names[1]


def test_images_of_a_call_that_read_no_file_are_told_apart():
    _, first, _, second, _ = _step(_reading(_result(_image("a"), _image("b"), of="u")))
    assert first["text"] != second["text"]
    assert "None" not in first["text"] + second["text"]


def test_each_message_keeps_its_own_images():
    request = _reading(_result(_image("a")))
    again = {
        "type": "tool_use",
        "id": "v",
        "name": "Read",
        "input": {"file_path": "b.png"},
    }
    request["messages"] += [
        {"role": "assistant", "content": [again]},
        {"role": "user", "content": [_result(_image("b"), of="v")]},
    ]

    steps = _asked(request)["messages"][2::2]

    assert [
        [block for block in step["content"] if block["type"] == "image"]
        for step in steps
    ] == [[_image("a")], [_image("b")]]
    assert "b.png" in steps[1]["content"][-2]["text"]


def test_a_call_with_an_odd_input_does_not_keep_images_from_moving():
    request = _reading(_result(_image("a")))
    request["messages"][1]["content"][-1]["input"] = "ls -la"
    assert _step(request)[-1] == _image("a")


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"[]",
        b"{}",
        json.dumps({"messages": [{"role": "user", "content": "plain"}]}).encode(),
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": [_image("a"), _result(_text("x"))]},
                    {"role": "assistant", "content": [_result(_image("b"))]},
                ]
            },
            indent=1,
        ).encode(),
        json.dumps(_reading(_result(_text("7" * jfc.RESULTS)))).encode(),
    ],
)
def test_a_request_with_nothing_to_cut_or_move_goes_as_it_came(body):
    assert _handler().ask(body) == body


def test_a_request_that_cannot_be_read_is_reported(capsys):
    _handler().ask(json.dumps(_reading(_result(_text("x")))).encode())
    assert not capsys.readouterr().out
    _handler().ask(b"not JSON")
    assert capsys.readouterr().out


@pytest.mark.parametrize(
    "text, tokens",
    [
        ("", 0),
        ("7" * 300, 300),
        ("12 34, 5.6\n", 11),
        ("the four lepton mass", 4),
        ("x" * 13, 3),
        ("    return counts\n", 4),
    ],
)
def test_a_text_is_sized_by_what_the_model_was_measured_to_make_of_it(text, tokens):
    assert jfc._tokens(text) == tokens


def _whole(text, of):
    """`text` as it stands after a cut, without the note that names `of`."""
    note = jfc.CUT.format(what=of)
    assert text.endswith(note)
    return text.removesuffix(note)


def test_a_result_past_its_share_loses_its_end_and_a_small_one_nothing():
    table, fit = "1234 " * 30_000, '{"mean": 125.02}'
    request = _reading(_result(_text(table), of="n"), _result(_text(fit), of="u"))

    cut, kept = _step(request)

    assert kept == request["messages"][2]["content"][1]
    rest = _whole(cut["content"][0]["text"], "notes.md")
    assert table.startswith(rest)
    room = jfc.RESULTS - jfc._tokens(fit)
    assert 0.99 * room < jfc._tokens(rest) <= room


def test_results_that_are_all_large_share_the_room_between_them():
    prose, table = "word " * 60_000, "1234 " * 30_000
    said = {"type": "tool_result", "tool_use_id": "u", "content": table}
    request = _reading(_result(_text(prose), of="n"), said, _text(prose))

    first, second, kept = _step(request)

    sizes = [
        jfc._tokens(_whole(first["content"][0]["text"], "notes.md")),
        jfc._tokens(_whole(second["content"], "this result")),
    ]
    assert kept == _text(prose)
    assert max(sizes) <= jfc.RESULTS // 2 + 1 and sum(sizes) > 0.99 * jfc.RESULTS


def test_a_result_dense_at_its_start_is_cut_until_it_fits():
    dense = "7" * 2 * jfc.RESULTS + "x " * 60 * jfc.RESULTS
    (cut,) = _step(_reading(_result(_text(dense), of="n")))
    assert jfc._tokens(_whole(cut["content"][0]["text"], "notes.md")) <= jfc.RESULTS


def test_an_earlier_step_is_cut_the_same_whatever_follows_it():
    request = _reading(_result(_text("1234 " * 30_000), of="n"))
    early = _asked(request)["messages"][2]
    request["messages"] += [
        {"role": "assistant", "content": [_text("More.")]},
        {"role": "user", "content": [_result(_text("5678 " * 30_000), of="n")]},
    ]

    later = _asked(request)["messages"]

    assert later[2] == early and later[4] != early
    assert later[4]["content"][0]["content"][0]["text"].endswith(
        jfc.CUT.format(what="notes.md")
    )


def test_the_note_of_a_moved_image_is_not_cut():
    large = _result(_text("word " * 60_000), of="n")
    shown, _, label, image = _step(_reading(_result(_image("a")), large))
    (note,) = shown["content"]
    assert label["text"].removesuffix(":") in note["text"] and image == _image("a")


REFUSAL = (
    b'{"detail":"litellm.ContextWindowExceededError: ... maximum context length '
    b'is 65536 tokens. However, you requested 12000 output tokens ..."}'
)
# How Claude Code takes the two sizes from the refusal of a long prompt.
SIZES = re.compile(r"prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)")


def _refused(*results, handler=None):
    """The two sizes Claude Code reads when a prompt of `results` is refused."""
    handler = handler or _handler()
    handler.ask(json.dumps(_reading(*results, max_tokens=jfc.OUTPUT)).encode())
    error = json.loads(handler.refusal(400, REFUSAL))["error"]
    assert error["type"] == "invalid_request_error"
    return tuple(map(int, SIZES.search(error["message"]).groups()))


def test_a_prompt_past_the_context_is_refused_the_way_claude_code_compacts_on():
    limit = jfc.CONTEXT - jfc.OUTPUT
    words = _text("word " * 2 * jfc.CONTEXT)
    data = "7" * 9 * jfc.CONTEXT
    paged = {"type": "document", "source": {"type": "base64", "data": data}}

    assert _refused() == (limit + 1, limit)
    long, _ = _refused(words)
    assert long == pytest.approx(2 * jfc.CONTEXT, rel=0.01)
    # Neither an image, moved or not, nor a document counts by its bytes.
    for carried in (_result(_image(data)), _image(data), _result(paged)):
        assert _refused(words, carried)[0] == pytest.approx(long, rel=0.01)


def test_the_size_refused_is_of_the_request_as_it_was_sent():
    size, limit = _refused(_result(_text("word " * 9 * jfc.CONTEXT), of="n"))
    assert size == limit + 1


def test_each_request_of_a_connection_is_refused_by_its_own_sizes():
    handler, limit = _handler(), jfc.CONTEXT - jfc.OUTPUT
    assert _refused(_text("word " * 2 * jfc.CONTEXT), handler=handler)[0] > 2 * limit
    assert _refused(handler=handler) == (limit + 1, limit)


@pytest.mark.parametrize(
    "status, body",
    [
        (400, b'{"detail":"Unexpected reasoning effort high."}'),
        (404, b'{"detail":"ContextWindowExceededError"}'),
        (500, b"upstream fell over"),
    ],
)
def test_any_other_refusal_reaches_claude_code_as_it_is(status, body):
    handler = _handler()
    handler.ask(json.dumps(_reading()).encode())
    assert handler.refusal(status, body) == body
    assert _handler().refusal(status, body) == body


def test_claude_talks_to_the_model_through_the_adapting_proxy(tmp_path, monkeypatch):
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(400)
            self.send_header("Content-Length", str(len(REFUSAL)))
            self.end_headers()
            self.wfile.write(REFUSAL)

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    address = ("127.0.0.1", upstream.server_address[1], False)
    monkeypatch.setattr(jfc, "Proxy", functools.partial(jfc.Proxy, upstream=address))
    request = _reading(_result(_image("a")))
    script = tmp_path / "claude.py"
    script.write_text(
        "import http.client, json, os, urllib.parse\n"
        "url = urllib.parse.urlsplit(os.environ['ANTHROPIC_BASE_URL'])\n"
        "client = http.client.HTTPConnection(url.netloc)\n"
        f"client.request('POST', url.path + '/v1/messages', {json.dumps(request)!r})\n"
        "reply = client.getresponse().read().decode()\n"
        "print(json.dumps({'type': 'result', 'is_error': False, 'result': reply}))\n"
    )

    try:
        said = jfc._claude([sys.executable, str(script)], tmp_path, tmp_path / "cfg")
    finally:
        upstream.shutdown()
        upstream.server_close()

    (sent,) = received
    assert json.loads(sent)["messages"][2]["content"][-1] == _image("a")
    assert SIZES.search(json.loads(said)["error"]["message"])


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
    return state


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
