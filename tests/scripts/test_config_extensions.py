"""Tests for the extension installer in docker/purdue-af/scripts/config-extensions.sh.

The interesting property is version reconciliation. The installer used to strip
the `@version` suffix before checking what was present, so a pinned spec was
only ever honoured on a cold home directory — once any build of an extension
was installed, the pin could never move it. That is how a session ended up on
openai.chatgpt 26.901.22334, which emits `using` declarations that the Node 22
bundled with code-server cannot parse, leaving the Codex view stuck loading.

code-server is stubbed: it reports a fixed extension list and records the argv
of every install, so the tests assert the exact commands a session would run.
"""

import re
import subprocess

import pytest
from common import REPO

SCRIPT = REPO / "docker" / "purdue-af" / "scripts" / "config-extensions.sh"

# Newest openai.chatgpt build verified to parse under code-server's Node 22.
PINNED_CHATGPT = "openai.chatgpt@26.820.71523"

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
    """The regression: a pinned spec must move an already-installed build."""
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


def test_chatgpt_is_pinned_in_the_script():
    """A floating openai.chatgpt install is what broke the Codex view."""
    line = re.search(
        r"^\s*_cs_install_if_missing\s+(openai\.chatgpt\S*)\s*$",
        SCRIPT.read_text(),
        re.MULTILINE,
    )
    assert line, "openai.chatgpt install line not found"
    assert line.group(1) == PINNED_CHATGPT


def _user_settings_section():
    """The block that writes ~/.jupyter/lab/user-settings, before code-server."""
    text = SCRIPT.read_text()
    start = text.index("TOPBAR_CONFIG_PATH=")
    end = text.index("# Pre-install code-server extensions")
    return text[start:end]


def test_root_owned_user_settings_are_repaired_before_the_first_write(tmp_path):
    """Images before 0.13.5 wrote user-settings as root and never chowned the
    grafana-iframe dir, so a home from that window has a root-owned
    plugin.jupyterlab-settings. Writing it as the user then fails, and the
    sourced hook takes the container down with it."""
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
        / ".jupyter/lab/user-settings/purdue-af-grafana-iframe"
        / "plugin.jupyterlab-settings"
    ).is_file()
