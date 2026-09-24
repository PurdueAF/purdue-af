"""Saved user_options that name a removed profile or choice must still spawn.

A spawn request without a body reuses the user's saved user_options. Options
saved by older releases name profiles by a slug derived from the versioned
display_name, such as purdue-af-0-12-3-pixi-based-environment-management, and
KubeSpawner refuses those with "No such profile" before any pod is created.

The spawner here is the real KubeSpawner configured with the real profileList
from values.yaml, and the hook is invoked through JupyterHub's own
_run_apply_user_options, in the order User.spawn uses before start().
"""

from types import SimpleNamespace
from typing import Any

import kubespawner.spawner
import pytest
import yaml
from common import REPO
from hub_helpers import load_snippet
from jupyterhub.objects import Hub
from kubespawner import KubeSpawner
from traitlets.config import Config

VALUES = REPO / "apps" / "jupyterhub" / "jupyterhub" / "values.yaml"

# Saved options as the hub DB holds them for users last spawned on older releases.
STALE_SAVED_OPTIONS = [
    {
        "profile": "purdue-af-0-12-3-pixi-based-environment-management",
        "cpu": "2",
        "gpu": "1",
        "memory": "2",
    },
    {
        "profile": "purdue-af-0-13-1-pixi-based-environment-management",
        "0-cpu": "3",
        "1-gpu": "1",
        "2-memory": "3",
        "3-interface": "2",
    },
]


def helm_rendered(value: Any) -> Any:
    """values.yaml as the hub reads it: Helm renders every map key as a string."""
    if isinstance(value, dict):
        return {str(k): helm_rendered(v) for k, v in value.items()}
    if isinstance(value, list):
        return [helm_rendered(v) for v in value]
    return value


PROFILE_LIST = helm_rendered(yaml.safe_load(VALUES.read_text()))["singleuser"][
    "profileList"
]
DEFAULT_PROFILE = next(p for p in PROFILE_LIST if p.get("default"))
GiB = 2**30
DEFAULT_IMAGE = DEFAULT_PROFILE["kubespawner_override"]["image"]


def make_spawner(monkeypatch, user_options, profile_list=None, hook=True):
    # the constructor loads a kube config and builds an API client; none is used
    monkeypatch.setattr(kubespawner.spawner, "load_config", lambda **kwargs: None)
    monkeypatch.setattr(kubespawner.spawner, "shared_client", lambda name: None)
    config = Config()
    config.KubeSpawner.profile_list = (
        PROFILE_LIST if profile_list is None else profile_list
    )
    if hook:
        snippet_config = load_snippet("custom-spawner.py", monkeypatch)["c"]
        config.KubeSpawner.apply_user_options = snippet_config["KubeSpawner"][
            "apply_user_options"
        ]
    user = SimpleNamespace(
        name="alice", id=1, url="/user/alice/", escaped_name="alice", orm_user=None
    )
    spawner = KubeSpawner(config=config, hub=Hub(), user=user, _mock=True)
    spawner.user_options = dict(user_options)
    return spawner


async def spawn_options(spawner):
    """The part of User.spawn + KubeSpawner.start that consumes user_options."""
    await spawner._run_apply_user_options(spawner.user_options)
    await spawner.load_user_options()


# ── the failure being fixed, reproduced without the hook ─────────────────────


@pytest.mark.parametrize("saved", STALE_SAVED_OPTIONS)
async def test_stale_profile_fails_without_the_hook(monkeypatch, saved):
    spawner = make_spawner(monkeypatch, saved, hook=False)
    with pytest.raises(ValueError, match="No such profile"):
        await spawn_options(spawner)


async def test_stale_choice_fails_without_the_hook(monkeypatch):
    spawner = make_spawner(
        monkeypatch, {"profile": DEFAULT_PROFILE["slug"], "1-gpu": "9"}, hook=False
    )
    with pytest.raises(KeyError):
        await spawn_options(spawner)


# ── with the hook ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("saved", STALE_SAVED_OPTIONS)
async def test_stale_profile_spawns_the_default_profile(monkeypatch, saved):
    spawner = make_spawner(monkeypatch, saved)
    await spawn_options(spawner)
    assert spawner.user_options["profile"] == DEFAULT_PROFILE["slug"]
    assert spawner.image == DEFAULT_IMAGE


async def test_valid_choices_survive_a_stale_profile(monkeypatch):
    spawner = make_spawner(monkeypatch, STALE_SAVED_OPTIONS[1])
    await spawn_options(spawner)
    assert spawner.cpu_guarantee == 32
    assert spawner.mem_guarantee == 64 * GiB
    assert spawner.extra_resource_limits == {"nvidia.com/mig-1g.5gb": 0}
    assert spawner.default_url.startswith("/vscode/")


async def test_legacy_option_keys_fall_back_to_default_choices(monkeypatch):
    spawner = make_spawner(monkeypatch, STALE_SAVED_OPTIONS[0])
    await spawn_options(spawner)
    assert spawner.cpu_guarantee == 4
    assert spawner.mem_guarantee == 16 * GiB
    assert spawner.extra_resource_limits == {"nvidia.com/mig-1g.5gb": 0}


@pytest.mark.parametrize("profile", [DEFAULT_PROFILE["slug"], None])
async def test_stale_choice_falls_back_to_its_default(monkeypatch, profile):
    saved = {"1-gpu": "9", "2-memory": "4"}
    if profile:
        saved["profile"] = profile
    spawner = make_spawner(monkeypatch, saved)
    await spawn_options(spawner)
    assert "1-gpu" not in spawner.user_options
    assert spawner.extra_resource_limits == {"nvidia.com/mig-1g.5gb": 0}
    assert spawner.mem_guarantee == 128 * GiB


async def test_integer_choice_matches_its_string_key(monkeypatch):
    spawner = make_spawner(
        monkeypatch, {"profile": DEFAULT_PROFILE["slug"], "2-memory": 3}
    )
    await spawn_options(spawner)
    assert spawner.mem_guarantee == 64 * GiB


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"profile": DEFAULT_PROFILE["slug"], "0-cpu": "4", "1-gpu": "2"},
        {"profile": "pre-release"},
        {"profile": "minimal-jupyterlab-interface"},
    ],
)
async def test_current_options_pass_unchanged(monkeypatch, options):
    spawner = make_spawner(monkeypatch, options)
    await spawn_options(spawner)
    assert spawner.user_options == options


async def test_async_profile_list_is_resolved(monkeypatch):
    # gpu-availability.py replaces profile_list with an async callable.
    async def profile_list(spawner):
        return PROFILE_LIST

    spawner = make_spawner(monkeypatch, STALE_SAVED_OPTIONS[0], profile_list)
    await spawn_options(spawner)
    assert spawner.image == DEFAULT_IMAGE


async def test_saved_options_are_not_mutated(monkeypatch):
    saved = dict(STALE_SAVED_OPTIONS[0])
    spawner = make_spawner(monkeypatch, saved)
    await spawner._run_apply_user_options(saved)
    assert saved == STALE_SAVED_OPTIONS[0]
