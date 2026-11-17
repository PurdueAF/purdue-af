"""The pieces of Claude Code's sandbox the AF image ships make it usable in a
session pod; turning it on stays the user's choice."""

import json

from common import REPO
from script_helpers import image_file

SETTINGS = REPO / "docker/purdue-af/agents/claude-sandbox.json"
INSTALLED = "/etc/claude-code/managed-settings.d/sandbox.json"
# A managed value of these turns the sandbox on, or takes a choice from the user.
FORCING = {
    "enabled",
    "failIfUnavailable",
    "allowUnsandboxedCommands",
    "autoAllowBashIfSandboxed",
    "allowManagedDomainsOnly",
    "allowManagedReadPathsOnly",
    "strictAllowlist",
}


def keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from keys(inner)


def test_the_image_installs_the_settings_as_a_managed_drop_in():
    assert image_file(INSTALLED) == SETTINGS


def test_the_settings_never_turn_the_sandbox_on_or_lock_it():
    settings = json.loads(SETTINGS.read_text())
    assert set(settings) == {"sandbox"}
    assert not FORCING & set(keys(settings))
    # Managed filesystem lists stop a user from switching that layer off.
    assert "filesystem" not in settings["sandbox"]
