"""Tests for docker/purdue-af/scripts/config-agents.sh — the startup hook that
points Claude Code, Codex and opencode at the AF MCP server and writes the
platform context into the file each of them reads automatically.

Nothing here reaches the cluster: `claude` and `codex` are replaced with stubs
that record their argv, so the tests assert the exact commands a session would
run. The property that matters most is that the session token is NEVER written
into a config file — it rotates every spawn, and the home directory is
persistent, so a baked-in token is stale the moment the session restarts."""

import os
import shlex
import subprocess

import pytest
from common import REPO

SCRIPT = REPO / "docker" / "purdue-af" / "scripts" / "config-agents.sh"
DOCKERFILE = REPO / "docker" / "purdue-af" / "Dockerfile"
SKILL_SOURCE = ".claude/skills/purdue-af-agentic-interface/SKILL.md"
CONTEXT = REPO / "docker/purdue-af/agents/platform-context.md"

STUB = """#!/bin/bash
printf '%s\\n' "$*" >> "$AGENT_LOG"
exit ${STUB_EXIT:-0}
"""


@pytest.fixture(scope="session")
def prepare_skill():
    from common import load_script

    return load_script(
        REPO / "docker/purdue-af/scripts/prepare-skill.py", "prepare_skill"
    )


@pytest.fixture()
def agent_home(tmp_path):
    """The session home the hook writes into."""
    home = tmp_path / "home" / "jovyan"
    home.mkdir(parents=True)
    return home


@pytest.fixture()
def run_script(tmp_path, agent_home):
    """Run the hook with stubbed agent CLIs; returns (result, [argv lines]).

    The hook addresses three paths that only exist inside the image: the
    session home, managed-block.py, and the platform context. The copy under
    test has exactly those redirected at the sandbox and the repo, so the files
    it produces can be asserted on directly. Nothing else is rewritten."""
    script = tmp_path / "config-agents.sh"
    script.write_text(
        SCRIPT.read_text()
        .replace(
            'NEW_HOME="/home/${NB_USER}"',
            f'NEW_HOME="{agent_home.parent}/${{NB_USER}}"',
        )
        .replace(
            "/usr/local/bin/managed-block.py",
            str(REPO / "docker/purdue-af/scripts/managed-block.py"),
        )
        .replace(
            '"/opt/purdue-af/agents/platform-context.md"',
            f'"{CONTEXT}"',
        )
    )

    def _run(tools=("claude", "codex"), stub_exit=0, **env):
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
    import json

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


def test_opencode_config_is_a_separate_layer_not_the_users_file(run_script, agent_home):
    """OPENCODE_CONFIG is merged between the user's global config and their
    project config, so the facility never edits a file the user owns. Writing
    into ~/.config/opencode/opencode.json would risk clobbering their settings
    — and could not be parsed safely at all if they wrote it as JSONC."""
    run_script()
    assert not (agent_home / ".config/opencode/opencode.json").exists()


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
    ".config/opencode/AGENTS.md",  # opencode
)


@pytest.mark.parametrize("relative", HARNESS_CONTEXT_FILES)
def test_context_is_written_for_every_harness(run_script, agent_home, relative):
    """The whole point of the feature: an agent started in a session knows the
    facility's rules without anyone telling it to look them up."""
    result, _ = run_script()
    assert result.returncode == 0
    written = (agent_home / relative).read_text()
    assert "Purdue Analysis Facility" in written
    # a guardrail from the context, not just the heading
    assert "refuse to run on a project under `/home/`" in written


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
    assert "Purdue Analysis Facility" in after


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


def test_server_name_matches_the_skill_and_repo_mcp_json(run_script):
    """The skill names the server explicitly, so a mismatch silently makes its
    instructions wrong for every session."""
    import json

    name = json.loads((REPO / ".mcp.json").read_text())["mcpServers"]
    (expected,) = name.keys()
    skill = (REPO / SKILL_SOURCE).read_text()
    assert expected in skill

    _, calls = run_script()
    assert all(expected in c for c in calls)


# --- the bundled skill ----------------------------------------------------


def test_skill_is_installed_into_the_claude_directory(run_script, tmp_path):
    result, _ = run_script()
    assert result.returncode == 0
    # no bundled skills on the test host — must degrade quietly, not fail
    assert "skipping" in result.stderr or "installed bundled skills" in result.stdout


def test_prepare_skill_replaces_the_laptop_setup_block(prepare_skill):
    source = (REPO / SKILL_SOURCE).read_text()
    out = prepare_skill.strip_setup_block(source)
    assert "One-time setup" not in out
    # the setup instructions are gone; the troubleshooting table still
    # references the laptop token paths, which is left alone deliberately —
    # this script rewrites the preamble, it does not edit prose
    assert "Get a JupyterHub API token" not in out
    assert "claude mcp add" not in out
    assert "Already set up" in out
    # frontmatter and body survive intact
    assert out.startswith("---") and "name: purdue-af-agentic-interface" in out
    assert "self-describing" in out


def test_prepare_skill_fails_loudly_if_the_preamble_changes(prepare_skill):
    """Better a red build than shipping laptop setup steps to every session."""
    with pytest.raises(SystemExit):
        prepare_skill.strip_setup_block("---\nname: x\n---\n\n# Title\n\nbody\n")


def test_skill_is_an_image_input():
    """It lives outside docker/purdue-af, so it needs an explicit entry or the
    content-addressed build would reuse a stale image after a skill edit."""
    out = subprocess.run(
        [str(REPO / ".github/workflows/image-inputs.sh"), "--paths", "purdue-af"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ},
    ).stdout
    assert SKILL_SOURCE.rsplit("/", 1)[0] in out


# --- the image actually ships what the hook needs ------------------------


def test_dockerfile_pins_every_agent_version():
    text = DOCKERFILE.read_text()
    for arg in ("CLAUDE_CODE_VERSION", "CODEX_VERSION", "OPENCODE_VERSION"):
        line = next(ln for ln in text.splitlines() if ln.startswith(f"ARG {arg}="))
        version = shlex.split(line.split("=", 1)[1])[0]
        assert version and version[0].isdigit(), f"{arg} must pin a version"


def test_dockerfile_installs_and_runs_the_hook():
    text = DOCKERFILE.read_text()
    assert "config-agents.sh" in text
    assert "prepare-skill.py" in text
    # the CLIs must be on PATH for both the terminal and the extensions
    assert "/opt/npm-global/bin" in text
    # and proven to run in the final image, not just installed
    for cli in ("claude", "codex", "opencode"):
        assert f"{cli} --version" in text, cli
    # xrdcp is promised on PATH by the platform context; prove it in the image
    assert "xrdcp --version" in text


def test_code_server_installs_both_agent_extensions():
    """Open VSX IDs — code-server does not use the MS marketplace."""
    text = (REPO / "docker/purdue-af/scripts/config-extensions.sh").read_text()
    assert "_cs_install_if_missing anthropic.claude-code" in text
    assert "_cs_install_if_missing openai.chatgpt" in text


def test_hook_is_registered_as_a_startup_script():
    text = DOCKERFILE.read_text()
    before = text.index("before-notebook.d")
    assert "config-agents.sh" in text[:before], (
        "config-agents.sh must be COPYd into before-notebook.d"
    )


def test_agent_files_are_image_inputs():
    """docker/purdue-af is an input path of the content-addressed image build,
    so editing the hook rebuilds the image rather than reusing a stale tag."""
    out = subprocess.run(
        [str(REPO / ".github/workflows/image-inputs.sh"), "--paths", "purdue-af"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ},
    ).stdout
    assert "docker/purdue-af" in out


# --- the managed section in user-owned agent files ------------------------


@pytest.fixture(scope="session")
def managed_block():
    from common import load_script

    return load_script(
        REPO / "docker/purdue-af/scripts/managed-block.py", "managed_block"
    )


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


def test_markers_warn_that_edits_are_overwritten(managed_block):
    assert "managed" in managed_block.BEGIN.lower()
    assert "overwrit" in managed_block.BEGIN.lower()


def test_startup_hook_targets_every_harness_context_file():
    """One file per harness, each the path that harness reads automatically at
    user scope — no skill, no prompt, no per-project setup."""
    text = (REPO / "docker/purdue-af/scripts/config-agents.sh").read_text()
    for target in (
        ".claude/CLAUDE.md",  # Claude Code
        ".codex/AGENTS.md",  # Codex
        ".config/opencode/AGENTS.md",  # opencode
    ):
        assert target in text, target


def test_cursor_is_documented_as_deliberately_unsupported():
    """Cursor has no user-scope instruction file: its rules are project-scoped
    and cross-project rules live in the Cursor UI, not on disk. Nothing here
    can reach it, so the omission is recorded rather than left to look like an
    oversight somebody 'fixes' with a file Cursor never reads."""
    hook = (REPO / "docker/purdue-af/scripts/config-agents.sh").read_text()
    assert "Cursor" in hook
    guide = (REPO / "docs/docs/guide-agentic-interface.md").read_text()
    assert "Cursor" in guide


def test_section_source_is_shipped_and_is_an_image_input():
    text = DOCKERFILE.read_text()
    assert "agents/platform-context.md" in text
    assert "managed-block.py" in text
    assert (REPO / "docker/purdue-af/agents/platform-context.md").is_file()


# --- the skill must not drift from the server -----------------------------


def _registered_tools():
    """Every @mcp.tool() in docker/agentic-interface/tools/."""
    import re

    names = set()
    for f in (REPO / "docker/agentic-interface/tools").glob("*.py"):
        lines = f.read_text().splitlines()
        for i, line in enumerate(lines):
            if "@mcp.tool(" not in line:
                continue
            for j in range(i + 1, min(i + 6, len(lines))):
                m = re.search(r"def\s+(\w+)\(", lines[j])
                if m:
                    names.add(m.group(1))
                    break
    return names


def test_skill_names_no_tool_that_does_not_exist():
    """The skill is hand-written prose; tool docstrings are the code. Any tool
    the skill names must still exist, or its guidance is quietly wrong."""
    import re

    tools = _registered_tools()
    assert tools, "no MCP tools found — the extractor broke"
    skill = (REPO / SKILL_SOURCE).read_text()
    # backtick-quoted identifiers that look like tool names
    mentioned = {
        m
        for m in re.findall(r"`(\w+)`", skill)
        if m.endswith(
            ("_session", "_clusters", "_cluster", "_logs", "_usage", "_profiles")
        )
        or m.startswith(
            ("start_", "stop_", "list_", "get_", "query_", "create_", "scale_", "wait_")
        )
    }
    assert mentioned, "skill names no tools at all — check the extraction"
    assert mentioned <= tools, (
        f"skill names non-existent tools: {sorted(mentioned - tools)}"
    )


def test_skill_does_not_re_document_tool_arguments():
    """Arguments and limits live in the tool descriptions, which cannot drift.
    Restating them in prose is what this cut removed — keep it removed."""
    skill = (REPO / SKILL_SOURCE).read_text()
    for marker in ("### Session lifecycle", "## Tool reference", "worker_cores"):
        assert marker not in skill, f"{marker!r} is duplicated tool documentation"


def test_skill_keeps_what_descriptions_cannot_carry():
    skill = (REPO / SKILL_SOURCE).read_text()
    assert "never `du`" in skill  # routing against the shell
    assert "kill the process you are running" in skill  # destructive warning
    # No failure-mode table: tool results and refusals are self-describing,
    # and a table here would drift from the messages in the code.
    assert "Authentication errors" not in skill


def test_bundled_python_scripts_do_not_use_the_system_interpreter():
    """Rocky 8 ships python3.6 at /usr/bin/python3, which cannot parse these
    scripts (`from __future__ import annotations` is a SyntaxError there). The
    image build runs before ENV PATH prefers the pixi env, and `su` resets PATH
    at session start — so both call sites must name the interpreter."""
    dockerfile = DOCKERFILE.read_text()
    build_step = next(
        ln for ln in dockerfile.splitlines() if "prepare-skill.py /tmp/skill" in ln
    )
    assert "BASE_ENV_DIR" in build_step, build_step

    hook = (REPO / "docker/purdue-af/scripts/config-agents.sh").read_text()
    call = next(ln for ln in hook.splitlines() if "managed-block.py" in ln)
    assert "${PYTHON}" in call, call
    assert "/opt/pixi/.pixi/envs/base-env/bin/python3" in hook


def test_bundled_python_scripts_target_the_platform_python():
    """They use 3.7+ syntax deliberately; this pins the reason down in one
    place so nobody 'fixes' the build by downgrading the scripts."""
    for name in ("prepare-skill.py", "managed-block.py"):
        text = (REPO / "docker/purdue-af/scripts" / name).read_text()
        assert "from __future__ import annotations" in text, name


# --- sourcing safety: start.sh runs these hooks with `source` ---------------


HOOK_DIR = REPO / "docker" / "purdue-af" / "scripts"
# the hooks Dockerfile installs into /usr/local/bin/before-notebook.d
STARTUP_HOOKS = ["config-agents.sh", "config-extensions.sh", "create-symlinks.sh"]


def test_start_sh_sources_hooks_rather_than_executing_them():
    """Pins the assumption the next two tests rest on. If upstream ever
    switches run-hooks to execute instead of source, these guards can relax."""
    start = (REPO / "docker/purdue-af/jupyter/start.sh").read_text()
    assert 'source "${f}"' in start


def test_sourcing_the_hook_returns_control_to_the_caller(tmp_path):
    """THE regression: a top-level `exit` in a sourced hook kills start.sh, so
    the container dies before JupyterLab launches — and every later hook is
    skipped. Reproduces run-hooks: source, then prove we are still alive."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("claude", "codex"):
        stub = bindir / tool
        stub.write_text("#!/bin/bash\nexit 0\n")
        stub.chmod(0o755)

    caller = tmp_path / "caller.sh"
    caller.write_text(f'source "{HOOK_DIR / "config-agents.sh"}"\necho STILL_ALIVE\n')
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
        f'source "{HOOK_DIR / "config-agents.sh"}"\n'
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


def _top_level_exits(text):
    """Lines calling `exit` outside any function definition.

    Indentation is no guide — an `exit` nested in an `if` is still top level,
    and that is exactly the form that killed a session start once already.
    Tracks function bodies by the `name() {` ... `}` pairs shfmt guarantees.
    """
    import re

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
        if re.match(r"^exit\b", stripped):
            offenders.append(f"{i}: {stripped}")
    return offenders


@pytest.mark.parametrize("hook", STARTUP_HOOKS)
def test_hooks_have_no_top_level_exit(hook):
    """Static counterpart to the sourcing test, applied to every startup hook.
    `exit` anywhere outside a function ends start.sh and the container with it —
    including on error paths, where it turns a recoverable problem into a
    session that never starts."""
    path = HOOK_DIR / hook
    if not path.is_file():
        pytest.skip(f"{hook} not present")
    offenders = _top_level_exits(path.read_text())
    assert not offenders, f"{hook} exits the sourcing shell at {offenders}"
