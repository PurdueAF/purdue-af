"""Tests for docker/purdue-af/agents/platform-context.md — the platform context
every agent in a session sees.

This file is hand-written prose stating hard numbers: session limits, storage
quotas, Dask worker caps, Slurm partitions. Those numbers live in the hub values
and the dask-gateway values, and prose cannot be regenerated from them. So each
test re-derives a fact from its source and asserts the context still says it —
a stale guardrail is worse than none, because an agent will act on it."""

import re

import yaml
from common import REPO

CONTEXT = REPO / "docker/purdue-af/agents/platform-context.md"
HUB_VALUES = REPO / "apps/jupyterhub/jupyterhub/values.yaml"
GATEWAYS = REPO / "apps/dask-gateway"


def context():
    return CONTEXT.read_text()


def _choices(option):
    values = yaml.safe_load(HUB_VALUES.read_text())
    profile = values["singleuser"]["profileList"][0]
    return [
        c["display_name"]
        for c in profile["profile_options"][option]["choices"].values()
    ]


def test_cpu_choices_match_the_spawn_form():
    for cores in _choices("0-cpu"):
        assert re.search(rf"\b{re.escape(cores)}\b", context()), cores


def test_memory_choices_match_the_spawn_form():
    for memory in _choices("2-memory"):
        number = memory.split()[0]
        assert re.search(rf"\b{number}\b", context()), memory


def test_gpu_choices_match_the_spawn_form():
    gpu = " ".join(_choices("1-gpu"))
    assert ("5GB" in gpu) == ("5 GB" in context() or "5GB" in context())
    assert ("40GB" in gpu) == ("40 GB" in context() or "40GB" in context())


def _row(path):
    """The context's storage-table row for a given path."""
    for line in context().splitlines():
        if line.startswith("|") and f"`{path}" in line:
            return line
    return ""


def test_home_quota_matches_the_hub_storage_capacity():
    """Asserted on the /home row itself: the number also appears in prose, so a
    loose substring check would pass even with the table wrong."""
    values = yaml.safe_load(HUB_VALUES.read_text())
    capacity = values["singleuser"]["storage"]["capacity"]  # e.g. "25Gi"
    number = re.match(r"(\d+)", capacity).group(1)
    row = _row("/home/")
    assert row, "no /home row in the storage table"
    assert f"{number} GB" in row, f"home quota is {capacity}, row says: {row}"


def test_storage_table_covers_every_volume_the_user_docs_list():
    """storage.md is the authoritative user-facing inventory; a volume it lists
    and this file omits is a volume the agent will not know about."""
    doc = (REPO / "docs/docs/storage.md").read_text()
    paths = set(re.findall(r"\| `(/[^`]+)` \|", doc))
    assert paths, "could not parse storage.md — check its table format"
    text = context()
    # compare on the fixed prefix: the docs write `/work/projects/` where this
    # file writes `/work/projects/<project>/`
    missing = [p for p in paths if p.split("<")[0] not in text]
    assert not missing, f"volumes documented for users but missing here: {missing}"


def test_every_mounted_path_is_documented():
    """A volume mounted into sessions but missing here is a volume the agent
    does not know exists."""
    values = yaml.safe_load(HUB_VALUES.read_text())
    mounts = {
        m["mountPath"].rstrip("/")
        for m in values["singleuser"]["storage"]["extraVolumeMounts"]
    }
    # munge is platform plumbing, not user-visible storage
    mounts -= {"/etc/secrets/munge"}
    text = context()
    missing = [m for m in mounts if m not in text]
    assert not missing, f"undocumented session mounts: {missing}"


def test_dask_worker_limits_match_each_gateway():
    """Cores and memory caps are enforced by the gateway; quoting a higher
    number here makes an agent propose a cluster that cannot be created."""
    text = context()
    for path in sorted(GATEWAYS.glob("dask-gateway-k8s*/values.yaml")):
        raw = path.read_text()
        cores = re.search(r'"worker_cores",[^)]*max=(\d+)', raw)
        memory = re.search(r'"worker_memory",[^)]*max=(\d+)', raw)
        if not cores or not memory:
            continue
        assert f"≤ {cores.group(1)} cores" in text, f"{path.parent.name}: cores"
        assert f"≤ {memory.group(1)} GiB per worker" in text, (
            f"{path.parent.name}: memory"
        )


def test_slurm_partitions_and_account_match_the_gateways():
    text = context()
    for path in sorted(GATEWAYS.glob("dask-gateway-k8s-slurm*/values.yaml")):
        raw = path.read_text()
        partition = re.search(r'"worker_partition":\s*"([^"]+)"', raw)
        account = re.search(r'"account":\s*"([^"]+)"', raw)
        if partition:
            assert f"`{partition.group(1)}`" in text, path.parent.name
        if account:
            assert f"account `{account.group(1)}`" in text, path.parent.name


def test_kubernetes_worker_ceiling_matches_the_user_docs():
    """The 200-worker figure is documented, not enforced in config — so its only
    source of truth is scaling-out.md."""
    doc = (REPO / "docs/docs/scaling-out.md").read_text()
    stated = re.search(r"up to (\d+) workers", doc)
    assert stated, "could not find the worker ceiling in scaling-out.md"
    assert f"≤ {stated.group(1)} workers" in context()


def test_session_ceiling_matches_the_user_docs():
    doc = (REPO / "docs/docs/scaling-out.md").read_text()
    stated = re.search(r"\*\*(\d+) CPU cores and (\d+) GB RAM\*\*", doc)
    assert stated, "could not find the session ceiling in scaling-out.md"
    cores, ram = stated.groups()
    assert f"{cores} cores / {ram} GB RAM" in context()


def test_one_cluster_per_user_limit_is_stated():
    enforced = any(
        "_max_active_clusters_per_user = 1" in p.read_text()
        for p in GATEWAYS.glob("dask-gateway-k8s*/values.yaml")
    )
    assert enforced
    assert "one active Dask Gateway cluster per user" in context()


def test_rules_and_guidance_are_distinguished():
    """Recommendations are welcome, but an agent has to be able to tell an
    enforced limit from a preference — otherwise it treats advice as a wall."""
    text = context()
    assert "**Rules.**" in text
    assert "**Guidance.**" in text
    header = text.split("### Session")[0]
    assert "enforced by the platform" in header and "deviate" in header


def test_pixi_home_block_matches_the_wrapper():
    """The wrapper refuses project and global commands under /home. An agent
    that does not know this will try `pixi init` in the home directory."""
    wrapper = (REPO / "docker/purdue-af/pixi-wrapper").read_text()
    assert "not allowed for projects under" in wrapper
    assert "not allowed under" in wrapper  # PIXI_HOME guard
    text = context()
    assert "refuse to run on a project under `/home/`" in text
    assert "PIXI_HOME" in text and "PIXI_CACHE_DIR" in text


def test_home_shortcuts_match_create_symlinks():
    """create-symlinks.sh is what actually makes them; a shortcut listed here
    that the script does not create sends the agent to a missing path."""
    script = (REPO / "docker/purdue-af/scripts/create-symlinks.sh").read_text()
    targets = set(re.findall(r"ln -sf? (\S+)", script))
    text = context()
    # (shortcut, target) pairs the script creates — assert the mapping, not just
    # the target: naming the wrong shortcut sends the agent to a missing path
    for shortcut, target in (
        ("~/work", "/work/"),
        ("~/eos-purdue", "/eos/purdue"),
        ("~/depot/users", "/depot/cms/users"),
    ):
        assert any(target.rstrip("/") in t for t in targets), f"{target} not linked"
        assert f"`{shortcut}` → `{target}`" in text, f"{shortcut} → {target} missing"


def test_xrootd_is_in_the_base_env_and_listed_on_path():
    """xrdcp is on the session PATH only because xrootd is a base-env
    dependency; if it is dropped, this file must stop promising it."""
    base = (REPO / "pixi/base/pixi.toml").read_text()
    assert re.search(r"^xrootd\s*=", base, re.M), "xrootd missing from pixi/base"
    text = context()
    on_path = text.split("**On PATH in a terminal:**")[1].split("\n\n")[0]
    assert "xrdcp" in on_path, f"xrdcp not in the on-PATH list: {on_path}"


def test_root_is_not_promised_from_the_bare_session():
    """ROOT belongs to an analysis environment, not the base image."""
    base = (REPO / "pixi/base/pixi.toml").read_text()
    assert not re.search(r"^root\s*=", base, re.M)
    assert "ROOT is deliberately not" in context()


def test_context_stays_within_a_sane_context_budget():
    """It is injected into every agent turn; unbounded growth is a real cost."""
    assert len(context().splitlines()) < 140


# --- dependency pins the platform context depends on ----------------------


def test_xrootd_is_capped_below_the_binding_split():
    """conda-forge ships no py312 build of `xrootd` 6.x — the Python bindings
    moved to `python-xrootd`. An unguarded Renovate bump would keep xrdcp
    working while silently removing `import XRootD`, so the cap must survive
    as long as the manifests pin a 5.x version."""
    renovate = (REPO / ".github/renovate.json5").read_text()
    manifests = [
        (REPO / "pixi/base/pixi.toml").read_text(),
        (REPO / "pixi/global/pixi.toml").read_text(),
    ]
    pinned_5x = any(re.search(r'^xrootd\s*=\s*"==5\.', m, re.M) for m in manifests)
    if not pinned_5x:
        return  # migrated to 6.x deliberately; the cap should have been lifted
    block = renovate[renovate.index("matchPackageNames: ['xrootd'") - 900 :]
    assert "allowedVersions: '<6'" in block
    for name in ("xrootd", "python-xrootd", "libxrootd"):
        assert f"'{name}'" in block, f"{name} not covered by the cap"
    # the reason has to travel with the pin, or it gets lifted blindly
    assert "python-xrootd" in block and "py312" in block
