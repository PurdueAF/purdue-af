"""Tests for docker/purdue-af/scripts/config-agents.sh. `claude` and `codex`
are stubs that record their argv; no config file may ever hold the token."""

import json
import re
import shlex
import subprocess

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
                    'SKILL_SRC="/opt/purdue-af/skills"': f'SKILL_SRC="{SKILLS}"',
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


def test_registers_with_both_agents(run_script):
    result, calls = run_script()
    assert result.returncode == 0, result.stderr
    assert any(c.startswith("mcp add --scope user --transport http") for c in calls)
    assert any(c.startswith("mcp add purdue-af-agentic-interface --url") for c in calls)


def test_token_is_never_written_into_a_config(run_script):
    """The header must carry the placeholder, not an expanded value: the agent
    resolves it per run against the session's own rotating token."""
    _, calls = run_script(JUPYTERHUB_API_TOKEN="super-secret-value")
    joined = "\n".join(calls)
    assert "super-secret-value" not in joined
    assert "Authorization: Bearer ${JUPYTERHUB_API_TOKEN}" in joined
    # Codex takes the env var by name rather than a header
    assert "--bearer-token-env-var JUPYTERHUB_API_TOKEN" in joined


def test_stale_entry_is_removed_before_adding(run_script):
    """`mcp add` is not idempotent — a re-run must not fail or duplicate."""
    _, calls = run_script()
    for tool_add in ("mcp add --scope user", "mcp add purdue-af-agentic-interface"):
        add = next(i for i, c in enumerate(calls) if c.startswith(tool_add))
        assert any(c.startswith("mcp remove") for c in calls[:add])


def _adds(calls):
    return [c for c in calls if "mcp add" in c]


def test_url_follows_the_namespace(run_script):
    _, calls = run_script(NAMESPACE="cms-other")
    adds = _adds(calls)
    assert len(adds) == 2
    assert all("agentic-interface.cms-other.svc.cluster.local:8888" in c for c in adds)


def test_url_defaults_to_the_production_namespace(run_script):
    _, calls = run_script()
    assert all(
        "agentic-interface.cms.svc.cluster.local:8888" in c for c in _adds(calls)
    )


def test_mcp_path_matches_the_service_prefix(run_script):
    """The service strips JUPYTERHUB_SERVICE_PREFIX itself, so the path has to
    carry it — /mcp alone 404s."""
    _, calls = run_script()
    assert all("/services/agentic-interface/mcp" in c for c in _adds(calls))


# --- opencode: a config layer of our own, never the user's file -----------


def _opencode_config(agent_home):
    path = agent_home / ".config" / "opencode" / "purdue-af.json"
    assert path.is_file(), "the hook wrote no opencode config"
    return json.loads(path.read_text())


def test_opencode_gets_the_mcp_server_without_a_cli(run_script, agent_home):
    """opencode has no `mcp add`, so registration is a file — which also means
    it does not depend on the CLI being installed at the moment the hook runs.
    A user who installs opencode into their own home later finds it wired."""
    result, _ = run_script(tools=("claude", "codex"))
    assert result.returncode == 0
    server = _opencode_config(agent_home)["mcp"]["purdue-af-agentic-interface"]
    assert server["type"] == "remote"
    assert server["enabled"] is True
    assert "/services/agentic-interface/mcp" in server["url"]


def test_opencode_persona_still_asks_before_editing_or_running(run_script, agent_home):
    """jupyter-ai ships its OpenCode persona with edit/bash set to "ask" and
    injects that as OPENCODE_CONFIG — but only when OPENCODE_CONFIG is unset,
    and the export in this hook means it never is. Restating the two settings
    is what keeps the JupyterLab chat asking before it edits a file or runs a
    command; drop them and the approval prompts disappear with no other sign."""
    run_script(tools=("claude", "codex", "opencode"))
    assert _opencode_config(agent_home)["permission"] == {
        "edit": "ask",
        "bash": "ask",
    }


def test_opencode_config_is_a_separate_layer_not_the_users_file(run_script, agent_home):
    """OPENCODE_CONFIG is merged between the user's global config and their
    project config, so the facility never edits a file the user owns. Writing
    into ~/.config/opencode/opencode.json would risk clobbering their settings
    — and could not be parsed safely at all if they wrote it as JSONC."""
    run_script()
    assert not (agent_home / ".config/opencode/opencode.json").exists()


def test_opencode_config_is_written_with_privileges_dropped(run_script):
    """~/.config lives in a persistent home the user controls between sessions,
    so they can replace it with a symlink before restarting. A root mkdir and
    redirect would follow that symlink, and a bare `chown` on it dereferences —
    handing the user ownership of whatever it points at. Everything under
    ~/.config is therefore written as the session user, and nothing chowns it
    back."""
    hook = SCRIPT.read_text()
    assert "_as_user \"mkdir -p '${NEW_HOME}/.config/opencode'" in hook, (
        "the opencode config must be written as the user, not as root"
    )
    # no chown anywhere in the hook may touch ~/.config
    for line in hook.splitlines():
        stripped = line.strip()
        if not stripped.startswith("chown"):
            continue
        assert stripped.startswith("chown -R"), (
            f"non-recursive chown dereferences symlinks: {stripped}"
        )
        assert "/.config" not in stripped, (
            f"nothing under ~/.config may be chowned, it is user-controlled: {stripped}"
        )


def test_opencode_context_is_not_also_written_as_a_file(run_script, agent_home):
    """opencode's `instructions` is documented in its schema as *additional*
    instruction files — additive to its own AGENTS.md lookup, not a fallback
    for it. Writing ~/.config/opencode/AGENTS.md as well would put the whole
    platform context into every turn twice."""
    run_script()
    assert not (agent_home / ".config/opencode/AGENTS.md").exists(), (
        "the context is delivered by `instructions`; a file here duplicates it"
    )


def test_opencode_instructions_are_omitted_when_the_context_is_missing(
    run_script, agent_home, tmp_path
):
    """Guarded like every other use of the context path: a config naming a file
    that is not there is worse than one that omits it."""
    result, _ = run_script(context=tmp_path / "absent.md")
    assert result.returncode == 0, result.stderr
    config = _opencode_config(agent_home)
    assert "instructions" not in config
    assert "mcp" in config, "the MCP server must still be registered"


@pytest.fixture(scope="session")
def managed_block():
    from common import load_script

    return load_script(MANAGED_BLOCK, "managed_block")


def test_opencode_config_is_exported_so_the_session_picks_it_up(run_script):
    """NAMESPACE is templated per deployment and the path depends on the
    session user, so this cannot be a Dockerfile ENV. start.sh sources the hook
    and then execs `sudo --preserve-env`, which carries the export through."""
    hook = SCRIPT.read_text()
    assert 'export OPENCODE_CONFIG="${OPENCODE_CFG}"' in hook
    start = (REPO / "docker/purdue-af/jupyter/start.sh").read_text()
    assert "--preserve-env" in start


def test_opencode_config_carries_the_platform_context(run_script, agent_home):
    """opencode's rules lookup is first-match-wins: a project AGENTS.md
    suppresses the global one. `instructions` is what keeps the guardrails
    present in an analysis repo that has its own AGENTS.md."""
    run_script()
    instructions = _opencode_config(agent_home)["instructions"]
    assert any("platform-context.md" in i for i in instructions)


def test_opencode_url_follows_the_namespace(run_script, agent_home):
    run_script(NAMESPACE="cms-other")
    url = _opencode_config(agent_home)["mcp"]["purdue-af-agentic-interface"]["url"]
    assert "agentic-interface.cms-other.svc.cluster.local:8888" in url


def test_opencode_config_never_stores_the_token(run_script, agent_home):
    """Same property as the other two harnesses: opencode expands `{env:...}`
    at connect time, so the rotating session token stays out of a file that
    lives in a persistent home directory."""
    run_script(JUPYTERHUB_API_TOKEN="super-secret-value")
    raw = (agent_home / ".config/opencode/purdue-af.json").read_text()
    assert "super-secret-value" not in raw
    assert "{env:JUPYTERHUB_API_TOKEN}" in raw


def test_unwritable_opencode_config_is_not_fatal(run_script, agent_home):
    """Never break a session start: a home restored read-only, or a stale
    root-owned ~/.config, must warn rather than take JupyterLab down."""
    (agent_home / ".config").mkdir()
    (agent_home / ".config").chmod(0o500)
    try:
        result, _ = run_script()
    finally:
        (agent_home / ".config").chmod(0o700)
    assert result.returncode == 0
    assert "WARNING" in result.stderr


# --- the platform context reaches every harness ---------------------------


HARNESS_CONTEXT_FILES = (
    ".claude/CLAUDE.md",  # Claude Code
    ".codex/AGENTS.md",  # Codex
    # opencode is served by `instructions` in its config layer, not a file here
    # — see test_opencode_context_is_not_also_written_as_a_file
)


@pytest.mark.parametrize("relative", HARNESS_CONTEXT_FILES)
def test_context_is_written_for_every_harness(run_script, agent_home, relative):
    """The whole point of the feature: an agent started in a session knows the
    facility's rules without anyone telling it to look them up."""
    result, _ = run_script()
    assert result.returncode == 0
    written = (agent_home / relative).read_text()
    assert CONTEXT.read_text().strip() in written


@pytest.mark.parametrize("relative", HARNESS_CONTEXT_FILES)
def test_a_users_own_instructions_survive(run_script, agent_home, relative):
    """These files belong to the user; only the block between the markers is
    ours. A session start must never cost them their own notes."""
    target = agent_home / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# My notes\n\nAlways use pytest -x.\n")
    run_script()
    after = target.read_text()
    assert "Always use pytest -x." in after
    assert CONTEXT.read_text().strip() in after


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


def test_server_name_matches_the_repo_mcp_json(run_script):
    import json

    name = json.loads((REPO / ".mcp.json").read_text())["mcpServers"]
    (expected,) = name.keys()

    _, calls = run_script()
    assert all(expected in c for c in calls)


# --- the bundled skill ----------------------------------------------------


def test_skill_is_installed_into_the_claude_directory(run_script, agent_home):
    result, _ = run_script()
    assert result.returncode == 0
    installed = agent_home / ".claude/skills" / SKILL
    assert installed.read_text() == (SKILLS / SKILL).read_text()


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
        (f"/opt/purdue-af/skills/{SKILL}", SKILLS / SKILL),
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
