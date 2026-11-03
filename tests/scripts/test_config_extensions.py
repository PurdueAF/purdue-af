"""Tests for the extension installer in docker/purdue-af/scripts/config-extensions.sh.

A pinned `id@version` must move an already-installed build of that extension.
code-server is stubbed: it reports a fixed extension list and records the argv
of every install, so the tests assert the exact commands a session would run.
"""

import json
import re
import subprocess

import pytest
from common import REPO

SCRIPT = REPO / "docker" / "purdue-af" / "scripts" / "config-extensions.sh"

# Mirrors the real CLI: `--list-extensions` prints bare ids, and only adding
# `--show-versions` appends `@version`. An installer that forgets the second
# flag therefore cannot tell builds apart — which was the bug.
STUB = """#!/bin/bash
listing=0
versions=0
for arg in "$@"; do
  [ "$arg" = "--list-extensions" ] && listing=1
  [ "$arg" = "--show-versions" ] && versions=1
done
if [ "$listing" = 1 ]; then
  if [ "$versions" = 1 ]; then
    cat "$INSTALLED_LIST"
  else
    sed 's/@.*//' "$INSTALLED_LIST"
  fi
  exit 0
fi
printf '%s\\n' "$*" >> "$INSTALL_LOG"
exit 0
"""


def _extract_function(name):
    """Return the body of a shell function from the script, brace-matched."""
    text = SCRIPT.read_text()
    start = text.index(f"{name}() {{")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


@pytest.fixture()
def run_install(tmp_path):
    """Invoke _cs_install_if_missing with a stubbed code-server.

    `installed` is the `--list-extensions --show-versions` output to report.
    Returns the list of install argv lines (empty when nothing was installed).
    """

    def _run(spec, installed=()):
        stub = tmp_path / "code-server"
        stub.write_text(STUB)
        stub.chmod(0o755)
        listing = tmp_path / "installed.txt"
        listing.write_text("".join(f"{line}\n" for line in installed))
        log = tmp_path / "install.log"
        log.write_text("")

        program = "\n".join(
            [
                'af_as_user() { "$@"; }',
                f'CODE_SERVER_BIN="{stub}"',
                f'CODE_EXTENSIONSDIR="{tmp_path}/ext"',
                f'CODE_USERDATADIR="{tmp_path}/data"',
                _extract_function("_cs_install_if_missing"),
                f'_cs_install_if_missing "{spec}"',
            ]
        )
        result = subprocess.run(
            ["bash", "-c", program],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "INSTALLED_LIST": str(listing),
                "INSTALL_LOG": str(log),
            },
        )
        assert result.returncode == 0, result.stderr
        return [line for line in log.read_text().splitlines() if line]

    return _run


def test_installs_when_absent(run_install):
    installs = run_install("openai.chatgpt@26.820.71523")
    assert len(installs) == 1
    assert "--install-extension openai.chatgpt@26.820.71523" in installs[0]


def test_skips_when_unpinned_and_present(run_install):
    assert run_install("ms-python.python", ["ms-python.python@2026.4.0"]) == []


def test_skips_when_pinned_version_matches(run_install):
    assert (
        run_install("openai.chatgpt@26.820.71523", ["openai.chatgpt@26.820.71523"])
        == []
    )


def test_reinstalls_when_pinned_version_differs(run_install):
    """A pinned spec moves an already-installed build."""
    installs = run_install(
        "openai.chatgpt@26.820.71523", ["openai.chatgpt@26.901.22334"]
    )
    assert len(installs) == 1
    assert "--install-extension openai.chatgpt@26.820.71523" in installs[0]
    # Downgrading over an existing install needs --force, or the CLI prompts.
    assert "--force" in installs[0]


def test_unrelated_extensions_do_not_satisfy_a_pin(run_install):
    installs = run_install(
        "openai.chatgpt@26.820.71523",
        ["openai.chatgpt-nightly@26.901.22334", "anthropic.claude-code@2.1.266"],
    )
    assert len(installs) == 1


def test_agent_extensions_are_installed_and_chatgpt_is_pinned():
    """A floating openai.chatgpt install is what broke the Codex view."""
    specs = re.findall(
        r"^\s*_cs_install_if_missing\s+(\S+)\s*$", SCRIPT.read_text(), re.MULTILINE
    )
    versions = dict(spec.partition("@")[::2] for spec in specs)
    assert "anthropic.claude-code" in versions
    assert versions["openai.chatgpt"], "openai.chatgpt must carry an @version"


def _user_settings_section():
    """The block that writes ~/.jupyter/lab/user-settings, before code-server."""
    text = SCRIPT.read_text()
    start = text.index("TOPBAR_CONFIG_PATH=")
    end = text.index("CODE_SERVER_BIN=")
    return text[start:end]


def test_root_owned_user_settings_are_repaired_before_the_first_write(tmp_path):
    """A root-owned user-settings file is chowned back before the user writes it."""
    home = tmp_path / "home" / "jovyan"
    home.mkdir(parents=True)
    log = tmp_path / "calls.log"
    log.write_text("")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    chown = bindir / "chown"
    chown.write_text('#!/bin/bash\nprintf \'chown %s\\n\' "$*" >> "$CALL_LOG"\n')
    chown.chmod(0o755)

    program = "\n".join(
        [
            'af_as_user() { printf \'as_user %s\\n\' "$*" >> "$CALL_LOG"; "$@"; }',
            f'NEW_HOME="{home}"',
            _user_settings_section(),
        ]
    )
    result = subprocess.run(
        ["bash", "-c", f"set -e\n{program}"],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "CALL_LOG": str(log),
            "NB_USER": "jovyan",
            "JUPYTER_IMAGE": "purdue-af:0.0.0",
            "HOSTNAME": "purdue-af-1",
        },
    )
    assert result.returncode == 0, result.stderr

    calls = [line for line in log.read_text().splitlines() if line]
    repair = f"chown -Rh jovyan:users {home}/.jupyter/lab/user-settings"
    assert repair in calls, calls
    assert calls.index(repair) < min(
        i for i, c in enumerate(calls) if c.startswith("as_user")
    ), calls
    assert (
        home
        / ".jupyter/lab/user-settings/jupyterlab-topbar-text"
        / "plugin.jupyterlab-settings"
    ).is_file()


def _retire_continue(tmp_path, home):
    """Run _retire_continue against `home` with a code-server that logs its arguments."""
    log = tmp_path / "code-server.log"
    code_server = tmp_path / "code-server"
    code_server.write_text('#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$CS_LOG"\n')
    code_server.chmod(0o755)
    program = "\n".join(
        [
            'af_as_user() { "$@"; }',
            f'NEW_HOME="{home}"',
            f'CODE_SERVER_BIN="{code_server}"',
            _extract_function("_retire_continue"),
            "_retire_continue",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", f"set -e\n{program}"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "CS_LOG": str(log)},
    )
    assert result.returncode == 0, result.stderr
    return log.read_text() if log.exists() else ""


def test_a_home_with_the_continue_extension_loses_it_and_its_key(tmp_path):
    home = tmp_path / "home" / "jovyan"
    extension = home / ".local/share/code-server/extensions/continue.continue-1.3.30"
    extension.mkdir(parents=True)
    (home / ".continue").mkdir()
    (home / ".continue/config.yaml").write_text("models: []\n")
    (home / ".continue/api-key.txt").write_text("secret")

    calls = _retire_continue(tmp_path, home)

    assert "--uninstall-extension continue.continue" in calls
    assert not (home / ".continue").exists()


def test_other_files_under_dot_continue_are_left(tmp_path):
    home = tmp_path / "home" / "jovyan"
    (home / ".continue/sessions").mkdir(parents=True)
    (home / ".continue/sessions/chat.json").write_text("{}")
    (home / ".continue/api-key.txt").write_text("secret")

    calls = _retire_continue(tmp_path, home)

    assert calls == ""
    assert not (home / ".continue/api-key.txt").exists()
    assert (home / ".continue/sessions/chat.json").is_file()


def test_a_home_without_continue_is_untouched(tmp_path):
    home = tmp_path / "home" / "jovyan"
    home.mkdir(parents=True)
    assert _retire_continue(tmp_path, home) == ""
    assert list(home.iterdir()) == []


# --- the bundled extension -------------------------------------------------

BUNDLED_ID = "purdueaf.purdue-af-interface-controls"


def _bundled_section():
    text = SCRIPT.read_text()
    return text[
        text.index("\t# The bundled extension") : text.index(
            "fi\n\n# A home that still"
        )
    ]


@pytest.fixture()
def run_bundled(tmp_path):
    """Run the bundled-extension block with a code-server that logs its arguments."""
    extensions = tmp_path / "ext"
    extensions.mkdir()
    image = tmp_path / "image"
    image.mkdir()
    vsix = image / "bundled.vsix"
    vsix.write_text("vsix")
    (image / "bundled.vsix.sha256").write_text("hash-1\n")
    log = tmp_path / "code-server.log"
    code_server = tmp_path / "code-server"

    def _run(exit_code=0):
        log.write_text("")
        code_server.write_text(
            f'#!/bin/bash\nprintf \'%s\\n\' "$*" >> "{log}"\n'
            f'case "$*" in *--install-extension*) exit {exit_code};; esac\n'
        )
        code_server.chmod(0o755)
        section = _bundled_section().replace(
            "/opt/purdue-af/code-server/purdue-af-interface-controls.vsix", str(vsix)
        )
        program = "\n".join(
            [
                'af_as_user() { "$@"; }',
                f'CODE_SERVER_BIN="{code_server}"',
                f'CODE_EXTENSIONSDIR="{extensions}"',
                f'CODE_USERDATADIR="{tmp_path}/data"',
                section,
                "echo SURVIVED",
            ]
        )
        result = subprocess.run(
            ["bash", "-c", f"set -e\n{program}"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert "SURVIVED" in result.stdout, result.stderr
        return log.read_text(), result

    return _run, extensions, image


def _register(extensions, stamp="hash-1\n"):
    (extensions / f"{BUNDLED_ID}-0.1.0").mkdir()
    (extensions / "extensions.json").write_text(
        json.dumps([{"identifier": {"id": BUNDLED_ID}}])
    )
    (extensions / ".purdue-af-interface-controls.sha256").write_text(stamp)


def test_the_first_start_installs_and_stamps(run_bundled):
    run, extensions, _ = run_bundled
    calls, _ = run()
    assert "--install-extension" in calls
    assert "--force" in calls
    stamp = extensions / ".purdue-af-interface-controls.sha256"
    assert stamp.read_text() == "hash-1\n"


def test_a_current_extension_costs_no_cli_call(run_bundled):
    run, extensions, _ = run_bundled
    _register(extensions)
    calls, _ = run()
    assert calls == ""


def test_a_new_build_is_reinstalled(run_bundled):
    run, extensions, _ = run_bundled
    _register(extensions, stamp="hash-0\n")
    calls, _ = run()
    assert f"--uninstall-extension {BUNDLED_ID}" in calls
    assert "--install-extension" in calls


def test_a_stamp_without_the_extension_does_not_count(run_bundled):
    """The stamp outlives an uninstall from the editor, which edits extensions.json."""
    run, extensions, _ = run_bundled
    _register(extensions)
    (extensions / "extensions.json").write_text("[]")
    calls, _ = run()
    assert "--install-extension" in calls


def test_a_registration_without_its_directory_does_not_count(run_bundled):
    run, extensions, _ = run_bundled
    _register(extensions)
    (extensions / f"{BUNDLED_ID}-0.1.0").rmdir()
    calls, _ = run()
    assert "--install-extension" in calls


def test_a_failed_install_is_reported_and_retried_next_start(run_bundled):
    run, extensions, _ = run_bundled
    _, result = run(exit_code=1)
    assert "ERROR" in result.stderr
    assert not (extensions / ".purdue-af-interface-controls.sha256").exists()
