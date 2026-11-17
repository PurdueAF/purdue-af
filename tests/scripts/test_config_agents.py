"""Tests for docker/purdue-af/scripts/config-agents.sh and the agent files the
image ships. `claude` and `codex` are stubs that record their argv; no config
file may ever hold the token."""

import json
import re
import shlex
import subprocess
import tomllib

import pytest
from common import REPO
from script_helpers import image_copies, image_file, sandboxed, startup_hooks

SCRIPT = REPO / "docker" / "purdue-af" / "scripts" / "config-agents.sh"
DOCKERFILE = REPO / "docker" / "purdue-af" / "Dockerfile"
SKILLS = REPO / ".claude" / "skills"
SKILL = "purdue-af-agentic-interface/SKILL.md"
CONTEXT = REPO / "docker/purdue-af/agents/platform-context.md"
MANAGED_BLOCK = REPO / "docker/purdue-af/scripts/managed-block.py"
SOURCE_HELPER = "source /usr/local/bin/af-as-user.sh"
HELPER = REPO / "docker/purdue-af/scripts/af-as-user.sh"
CODEX_CONFIG = REPO / "docker/purdue-af/agents/codex-config.toml"
OPENCODE_LAYER = REPO / "docker/purdue-af/agents/opencode.json"
MCP_NAME = "purdue-af-agentic-interface"
MCP_PATH = "/services/agentic-interface/mcp"

STUB = """#!/bin/bash
printf '%s\\n' "$*" >> "$AGENT_LOG"
exit ${STUB_EXIT:-0}
"""


@pytest.fixture()
def agent_home(tmp_path):
    """The session home the hook writes into."""
    home = tmp_path / "home" / "jovyan"
    home.mkdir(parents=True)
    return home


@pytest.fixture()
def run_script(tmp_path, agent_home):
    """Run the hook with stubbed agent CLIs; returns (result, [argv lines]).

    The paths that only exist inside the image are redirected at the sandbox
    and the repo, so the files the hook produces can be asserted on directly.
    Nothing else is rewritten."""

    def _run(tools=("claude", "codex"), stub_exit=0, context=CONTEXT, **env):
        script = tmp_path / "config-agents.sh"
        script.write_text(
            sandboxed(
                SCRIPT,
                {
                    SOURCE_HELPER: f"source {HELPER}",
                    'NEW_HOME="/home/${NB_USER}"': (
                        f'NEW_HOME="{agent_home.parent}/${{NB_USER}}"'
                    ),
                    "/usr/local/bin/managed-block.py": str(MANAGED_BLOCK),
                    '"/opt/purdue-af/agents/platform-context.md"': f'"{context}"',
                },
            )
        )
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        log = tmp_path / "agent.log"
        log.write_text("")
        for tool in tools:
            stub = bindir / tool
            stub.write_text(STUB)
            stub.chmod(0o755)
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{bindir}:/usr/bin:/bin",
                "HOME": str(tmp_path),
                "NB_USER": "jovyan",
                "AGENT_LOG": str(log),
                "STUB_EXIT": str(stub_exit),
                **env,
            },
        )
        calls = [ln for ln in log.read_text().splitlines() if ln.strip()]
        return result, calls

    return _run


def test_registers_with_claude_code(run_script):
    result, calls = run_script()
    assert result.returncode == 0, result.stderr
    assert any(c.startswith("mcp add --scope user --transport http") for c in calls)


def test_token_is_never_written_into_a_config(run_script):
    """The header must carry the placeholder, not an expanded value: the agent
    resolves it per run against the session's own rotating token."""
    _, calls = run_script(JUPYTERHUB_API_TOKEN="super-secret-value")
    joined = "\n".join(calls)
    assert "super-secret-value" not in joined
    assert "Authorization: Bearer ${JUPYTERHUB_API_TOKEN}" in joined


def test_stale_entry_is_removed_before_adding(run_script):
    """`mcp add` is not idempotent — a re-run must not fail or duplicate."""
    _, calls = run_script()
    add = next(i for i, c in enumerate(calls) if c.startswith("mcp add --scope user"))
    assert any(c.startswith("mcp remove") for c in calls[:add])


def _adds(calls):
    return [c for c in calls if "mcp add" in c]


def test_url_follows_the_namespace(run_script):
    _, calls = run_script(NAMESPACE="cms-other")
    (add,) = _adds(calls)
    assert "agentic-interface.cms-other.svc.cluster.local:8888" in add


def test_url_defaults_to_the_production_namespace(run_script):
    _, calls = run_script()
    (add,) = _adds(calls)
    assert "agentic-interface.cms.svc.cluster.local:8888" in add


def test_mcp_path_matches_the_service_prefix(run_script):
    """The service strips JUPYTERHUB_SERVICE_PREFIX itself, so the path has to
    carry it — /mcp alone 404s."""
    _, calls = run_script()
    (add,) = _adds(calls)
    assert MCP_PATH in add


# --- Codex: the server and the skill are image files -----------------------


def _codex_server():
    ((name, server),) = tomllib.loads(CODEX_CONFIG.read_text())["mcp_servers"].items()
    assert name == MCP_NAME
    return server


def test_codex_reads_the_mcp_server_from_its_system_config():
    assert image_file("/etc/codex/config.toml") == CODEX_CONFIG
    assert _codex_server()["url"].endswith(MCP_PATH)


def test_codex_config_names_the_token_variable_not_the_token():
    """Codex reads the variable at connect time, so the image file fits every
    session and its rotating token."""
    server = _codex_server()
    assert server["bearer_token_env_var"] == "JUPYTERHUB_API_TOKEN"
    assert set(server) == {"url", "bearer_token_env_var"}


def test_a_codex_entry_left_in_the_home_is_removed(run_script, agent_home):
    """A user-level entry of the same name outranks the system one, so one an
    earlier image wrote would pin its address."""
    config = agent_home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(f'[mcp_servers.{MCP_NAME}]\nurl = "http://old"\n')
    _, calls = run_script()
    assert f"mcp remove {MCP_NAME}" in calls


def test_a_home_without_the_codex_entry_is_left_alone(run_script, agent_home):
    config = agent_home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.mine]\nurl = "http://mine"\n')
    _, calls = run_script()
    assert not any(c == f"mcp remove {MCP_NAME}" for c in calls)
    assert config.read_text() == '[mcp_servers.mine]\nurl = "http://mine"\n'


# --- opencode: a config layer of the image, never the user's file ----------


def _opencode_layer():
    return json.loads(OPENCODE_LAYER.read_text())


def _env(name):
    """The value the Dockerfile's ENV gives a variable."""
    match = re.search(rf'(?m)^ENV {name}="([^"]+)"$', DOCKERFILE.read_text())
    assert match, f"the Dockerfile sets no {name}"
    return match.group(1)


def test_opencode_layer_is_the_file_the_session_environment_names():
    """Every process of the session inherits the image's environment, so the
    layer reaches terminals, the JupyterLab persona and the VS Code extension."""
    assert image_file(_env("OPENCODE_CONFIG")) == OPENCODE_LAYER


def test_opencode_layer_is_not_the_system_config():
    """opencode's system config in /etc/opencode outranks a user's own files,
    which would hold everyone to this layer's permissions; a file named by
    OPENCODE_CONFIG is merged below the user's project config."""
    assert not _env("OPENCODE_CONFIG").startswith("/etc/opencode")


def test_opencode_gets_the_mcp_server():
    server = _opencode_layer()["mcp"][MCP_NAME]
    assert server["type"] == "remote"
    assert server["enabled"] is True
    assert server["url"].endswith(MCP_PATH)


def test_opencode_persona_still_asks_before_editing_or_running():
    """jupyter-ai ships its OpenCode persona with edit/bash set to "ask" and
    injects that as OPENCODE_CONFIG — but only when OPENCODE_CONFIG is unset,
    and the image sets it. Restating the two settings is what keeps the
    JupyterLab chat asking before it edits a file or runs a command; drop them
    and the approval prompts disappear with no other sign."""
    assert _opencode_layer()["permission"] == {"edit": "ask", "bash": "ask"}


def test_opencode_layer_never_stores_the_token():
    """opencode expands `{env:...}` at connect time."""
    headers = _opencode_layer()["mcp"][MCP_NAME]["headers"]
    assert headers == {"Authorization": "Bearer {env:JUPYTERHUB_API_TOKEN}"}


def test_opencode_layer_carries_the_platform_context():
    """opencode's rules lookup is first-match-wins: a project AGENTS.md
    suppresses the global one. `instructions` is what keeps the guardrails
    present in an analysis repo that has its own AGENTS.md."""
    (instructions,) = _opencode_layer()["instructions"]
    assert image_file(instructions) == CONTEXT


def test_opencode_layer_carries_the_skill():
    (skills,) = _opencode_layer()["skills"]
    assert image_file(f"{skills}/{SKILL}") == SKILLS / SKILL


def test_codex_and_opencode_reach_the_same_address():
    """A service name without a namespace resolves in the session's own, so
    one image file fits every deployment."""
    url = _codex_server()["url"]
    assert url == _opencode_layer()["mcp"][MCP_NAME]["url"]
    assert url == f"http://agentic-interface:8888{MCP_PATH}"


def test_an_opencode_layer_left_in_the_home_is_removed(run_script, agent_home):
    config = agent_home / ".config" / "opencode"
    config.mkdir(parents=True)
    (config / "purdue-af.json").write_text("{}")
    (config / "opencode.json").write_text('{"theme": "mine"}')
    result, _ = run_script()
    assert result.returncode == 0, result.stderr
    assert not (config / "purdue-af.json").exists()
    assert (config / "opencode.json").read_text() == '{"theme": "mine"}'


@pytest.fixture(scope="session")
def managed_block():
    from common import load_script

    return load_script(MANAGED_BLOCK, "managed_block")


# --- the platform context reaches every harness ---------------------------


def test_claude_code_reads_the_context_from_its_managed_policy_file():
    assert image_file("/etc/claude-code/CLAUDE.md") == CONTEXT


def test_context_is_written_for_codex(run_script, agent_home):
    """Codex reads no system-level instruction file, so the context goes into
    the user's own."""
    result, _ = run_script()
    assert result.returncode == 0
    written = (agent_home / ".codex/AGENTS.md").read_text()
    assert CONTEXT.read_text().strip() in written


def test_a_users_own_codex_instructions_survive(run_script, agent_home):
    """The file belongs to the user; only the block between the markers is
    ours. A session start must never cost them their own notes."""
    target = agent_home / ".codex/AGENTS.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# My notes\n\nAlways use pytest -x.\n")
    run_script()
    after = target.read_text()
    assert "Always use pytest -x." in after
    assert CONTEXT.read_text().strip() in after


def test_the_context_left_in_a_claude_home_is_removed(
    run_script, agent_home, managed_block
):
    """With the block still in ~/.claude/CLAUDE.md, Claude Code would read the
    context twice."""
    target = agent_home / ".claude/CLAUDE.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        managed_block.apply_block("# My notes\n\nAlways use pytest -x.\n", "old AF\n")
    )
    result, _ = run_script()
    assert result.returncode == 0, result.stderr
    assert target.read_text() == "# My notes\n\nAlways use pytest -x.\n"


def test_a_claude_file_holding_only_the_context_is_removed(
    run_script, agent_home, managed_block
):
    target = agent_home / ".claude/CLAUDE.md"
    target.parent.mkdir(parents=True)
    target.write_text(managed_block.apply_block("", "old AF\n"))
    run_script()
    assert not target.exists()


def test_a_users_own_claude_instructions_are_left_alone(run_script, agent_home):
    target = agent_home / ".claude/CLAUDE.md"
    target.parent.mkdir(parents=True)
    target.write_text("# My notes\n")
    run_script()
    assert target.read_text() == "# My notes\n"


# --- never break a session start -----------------------------------------


def test_missing_cli_is_not_fatal(run_script):
    result, calls = run_script(tools=())
    assert result.returncode == 0
    assert not calls
    assert "not found" in result.stderr


def test_failing_cli_is_not_fatal(run_script):
    result, _ = run_script(stub_exit=1)
    assert result.returncode == 0
    assert "WARNING" in result.stderr


def test_missing_nb_user_is_not_fatal(run_script):
    result, calls = run_script(NB_USER="")
    assert result.returncode == 0
    assert not calls


def test_unwritable_home_is_not_fatal(run_script, agent_home):
    """Never break a session start: a home restored read-only must warn rather
    than take JupyterLab down."""
    agent_home.chmod(0o500)
    try:
        result, _ = run_script()
    finally:
        agent_home.chmod(0o700)
    assert result.returncode == 0
    assert "WARNING" in result.stderr


def test_server_name_matches_the_repo_mcp_json(run_script):
    (expected,) = json.loads((REPO / ".mcp.json").read_text())["mcpServers"].keys()
    assert expected == MCP_NAME

    _, calls = run_script()
    assert calls and all(expected in c for c in calls)


# --- the bundled skill ----------------------------------------------------


@pytest.mark.parametrize(
    "directory",
    [
        "/etc/claude-code/.claude/skills",  # Claude Code
        "/etc/codex/skills",  # Codex
        "/opt/purdue-af/skills",  # opencode, through its config layer
    ],
)
def test_the_skill_is_where_each_agent_reads_system_skills(directory):
    assert image_file(f"{directory}/{SKILL}") == SKILLS / SKILL


def test_the_skill_copy_left_in_a_claude_home_is_removed(run_script, agent_home):
    """Claude Code would list the skill twice, and opencode, which reads
    ~/.claude/skills too, a second copy that no longer follows the image."""
    skills = agent_home / ".claude/skills"
    (skills / MCP_NAME).mkdir(parents=True)
    (skills / SKILL).write_text("old")
    (skills / "mine").mkdir()
    (skills / "mine" / "SKILL.md").write_text("mine")
    run_script()
    assert not (skills / MCP_NAME).exists()
    assert (skills / "mine" / "SKILL.md").read_text() == "mine"


def test_every_file_the_image_copies_is_an_image_input():
    """Outside the inputs, the content-addressed build would reuse a stale image
    after an edit to that file."""
    inputs = subprocess.run(
        [str(REPO / ".github/workflows/image-inputs.sh"), "--paths", "purdue-af"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    for sources, _ in image_copies():
        for source in sources:
            assert source.startswith(tuple(inputs)), source


# --- the image actually ships what the hook needs ------------------------


def test_dockerfile_pins_every_agent_version():
    text = DOCKERFILE.read_text()
    for arg in ("CLAUDE_CODE_VERSION", "CODEX_VERSION", "OPENCODE_VERSION"):
        line = next(ln for ln in text.splitlines() if ln.startswith(f"ARG {arg}="))
        version = shlex.split(line.split("=", 1)[1])[0]
        assert version and version[0].isdigit(), f"{arg} must pin a version"


@pytest.mark.parametrize(
    "path, source",
    [
        ("/usr/local/bin/managed-block.py", MANAGED_BLOCK),
        ("/opt/purdue-af/agents/platform-context.md", CONTEXT),
    ],
)
def test_the_image_ships_what_the_hook_reads(path, source):
    assert image_file(path) == source
    assert source.read_text().strip()


# --- the managed section in user-owned agent files ------------------------


SECTION = "## Purdue Analysis Facility\n\nAF content.\n"


def test_block_is_appended_to_an_empty_file(managed_block):
    out = managed_block.apply_block("", SECTION)
    assert out.startswith(managed_block.BEGIN)
    assert out.rstrip().endswith(managed_block.END)
    assert "AF content." in out


def test_user_content_is_preserved(managed_block):
    """The whole point: users own these files, we own one section of them."""
    existing = "# My notes\n\nAlways use pytest -x.\n"
    out = managed_block.apply_block(existing, SECTION)
    assert "# My notes" in out
    assert "Always use pytest -x." in out
    assert managed_block.BEGIN in out


def test_updating_replaces_only_the_managed_section(managed_block):
    first = managed_block.apply_block("# Mine\n\nkeep me\n", "old AF content\n")
    second = managed_block.apply_block(first, "new AF content\n")
    assert "keep me" in second
    assert "old AF content" not in second
    assert "new AF content" in second
    assert second.count(managed_block.BEGIN) == 1


def test_content_after_the_block_survives(managed_block):
    """A user who writes below our section must not lose it."""
    seeded = managed_block.apply_block("", "old\n")
    with_tail = seeded + "\n## My own section\n\nmine\n"
    out = managed_block.apply_block(with_tail, "new\n")
    assert "## My own section" in out and "mine" in out
    assert "new" in out and "old" not in out


def test_is_idempotent(managed_block):
    once = managed_block.apply_block("# Mine\n\nkeep\n", SECTION)
    twice = managed_block.apply_block(once, SECTION)
    assert once == twice


def test_stray_end_marker_before_the_block_does_not_duplicate_it(managed_block):
    """An END marker above the block does not make the block look absent."""
    existing = f"# Mine\n{managed_block.END}\n"
    once = managed_block.apply_block(existing, SECTION)
    twice = managed_block.apply_block(once, SECTION)
    assert once == twice
    assert twice.count(managed_block.BEGIN) == 1
    assert twice.startswith(existing)


def test_removing_takes_only_the_managed_section(managed_block):
    seeded = managed_block.apply_block("# Mine\n\nkeep\n", SECTION)
    with_tail = seeded + "\n## My own section\n\nmine\n"
    out = managed_block.remove_block(with_tail)
    assert out == "# Mine\n\nkeep\n\n## My own section\n\nmine\n"
    assert managed_block.remove_block(out) == out


def test_removing_from_a_file_of_only_the_section_leaves_nothing(managed_block):
    assert managed_block.remove_block(managed_block.apply_block("", SECTION)) == ""


def test_an_unfinished_block_is_not_removed(managed_block):
    """Without its end marker there is no telling where the user's text resumes."""
    existing = f"# Mine\n{managed_block.BEGIN}\nhalf\n"
    assert managed_block.remove_block(existing) == existing


def run_managed_block(managed_block, monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["managed-block.py", *map(str, argv)])
    return managed_block.main()


def test_cli_writes_then_reports_current(managed_block, monkeypatch, tmp_path, capsys):
    section = tmp_path / "section.md"
    section.write_text(SECTION)
    target = tmp_path / "new" / "dir" / "AGENTS.md"
    assert run_managed_block(managed_block, monkeypatch, section, target) == 0
    assert "AF content." in target.read_text()
    assert "updated" in capsys.readouterr().out
    run_managed_block(managed_block, monkeypatch, section, target)
    assert "already current" in capsys.readouterr().out


def test_cli_removes_the_section_and_an_emptied_file(
    managed_block, monkeypatch, tmp_path
):
    section = tmp_path / "section.md"
    section.write_text(SECTION)
    kept, emptied = tmp_path / "kept.md", tmp_path / "emptied.md"
    kept.write_text("# Mine\n")
    for target in (kept, emptied):
        run_managed_block(managed_block, monkeypatch, section, target)
        assert run_managed_block(managed_block, monkeypatch, "--remove", target) == 0
    assert kept.read_text() == "# Mine\n"
    assert not emptied.exists()
    assert run_managed_block(managed_block, monkeypatch, "--remove", emptied) == 0


def test_cli_rejects_bad_arguments(managed_block, monkeypatch):
    with pytest.raises(SystemExit):
        run_managed_block(managed_block, monkeypatch, "only-one")


def test_bundled_python_scripts_do_not_use_the_system_interpreter():
    """Rocky 8 ships python3.6 at /usr/bin/python3, so the call site names the
    interpreter."""
    hook = SCRIPT.read_text()
    call = next(ln for ln in hook.splitlines() if "managed-block.py" in ln)
    assert "${PYTHON}" in call, call
    assert "/opt/pixi/.pixi/envs/base-env/bin/python3" in hook


# --- sourcing safety: start.sh runs these hooks with `source` ---------------


STARTUP_HOOKS = startup_hooks()


def _sourceable(tmp_path):
    """The hook as the image has it, with its helper found in the repo."""
    hook = tmp_path / "hook.sh"
    hook.write_text(sandboxed(SCRIPT, {SOURCE_HELPER: f"source {HELPER}"}))
    return hook


def test_startup_hooks_are_read_from_the_dockerfile():
    """An empty list would skip the scan below with CI still green."""
    assert SCRIPT in STARTUP_HOOKS


def test_start_sh_sources_hooks_rather_than_executing_them():
    """Pins the assumption the next two tests rest on. If upstream ever
    switches run-hooks to execute instead of source, these guards can relax."""
    start = (REPO / "docker/purdue-af/jupyter/start.sh").read_text()
    assert 'source "${f}"' in start


def test_sourcing_the_hook_returns_control_to_the_caller(tmp_path):
    """Sourced as run-hooks sources it, the hook returns to its caller."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("claude", "codex"):
        stub = bindir / tool
        stub.write_text("#!/bin/bash\nexit 0\n")
        stub.chmod(0o755)

    caller = tmp_path / "caller.sh"
    caller.write_text(f'source "{_sourceable(tmp_path)}"\necho STILL_ALIVE\n')
    result = subprocess.run(
        ["bash", str(caller)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "NB_USER": "jovyan",
        },
    )
    assert "STILL_ALIVE" in result.stdout, (
        "sourcing the hook terminated the calling shell — start.sh would exit "
        f"and the container would never start.\nstdout: {result.stdout}"
    )
    assert result.returncode == 0


def test_sourced_hooks_do_not_leak_shell_options(tmp_path):
    """`set -e`/`set -u` at the top level of a sourced hook applies to the rest
    of start.sh, turning any later unset variable into a container failure."""
    caller = tmp_path / "caller.sh"
    caller.write_text(
        f'source "{_sourceable(tmp_path)}"\n'
        'case "$-" in *e*) echo LEAKED_E ;; esac\n'
        'case "$-" in *u*) echo LEAKED_U ;; esac\n'
        "echo DONE\n"
    )
    result = subprocess.run(
        ["bash", str(caller)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "NB_USER": ""},
    )
    assert "DONE" in result.stdout
    assert "LEAKED_E" not in result.stdout
    assert "LEAKED_U" not in result.stdout


def _top_level(text, command):
    """Lines starting with the command regex outside any function definition.

    Indentation is no guide — an `exit` nested in an `if` is still top level,
    and that is exactly the form that killed a session start once already.
    Tracks function bodies by the `name() {` ... `}` pairs shfmt guarantees.
    """
    offenders, depth = [], 0
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{", stripped):
            depth += 1
            continue
        if depth and stripped == "}":
            depth -= 1
            continue
        if depth or stripped.startswith("#"):
            continue
        if re.match(command, stripped):
            offenders.append(f"{i}: {stripped}")
    return offenders


@pytest.mark.parametrize("hook", STARTUP_HOOKS, ids=lambda hook: hook.name)
def test_hooks_have_no_top_level_exit_or_shell_option(hook):
    """Static counterpart to the sourcing tests, applied to every startup hook.
    `exit` anywhere outside a function ends start.sh and the container with it —
    including on error paths, where it turns a recoverable problem into a
    session that never starts — and `set -e`/`set -u` outlive the hook."""
    assert hook.is_file(), f"the Dockerfile installs {hook}, which is not in the repo"
    text = hook.read_text()
    exits = _top_level(text, r"exit\b")
    assert not exits, f"{hook.name} exits the sourcing shell at {exits}"
    options = _top_level(text, r"set\s+-[a-z]*[eu]")
    assert not options, f"{hook.name} leaks shell options at {options}"
