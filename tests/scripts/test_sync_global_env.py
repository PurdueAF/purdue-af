"""Tests for apps/af-utils/pixi-global-sync/sync-global-env.py — the daemon
that keeps /work/pixi/global in sync with the repo. Exercises the pure
logic (drift detection, pause, atomic manifest staging, lock takeover) on
tmp filesystems; the pixi install itself is validated upstream by
ci-pixi-global.yml and post-checked in production by check-env."""

import json
import os
import time

import pytest
from common import REPO, load_script


@pytest.fixture()
def sync(tmp_path):
    mod = load_script(
        REPO / "apps" / "af-utils" / "pixi-global-sync" / "sync-global-env.py",
        "sync_global_env",
    )
    mod.WORK_ROOT = tmp_path / "work" / "pixi"
    mod.LIVE_DIR = mod.WORK_ROOT / "global"
    mod.CACHE_DIR = mod.WORK_ROOT / ".cache"
    mod.LOCK_DIR = mod.CACHE_DIR / ".sync-lock"
    mod.LIVE_DIR.mkdir(parents=True)
    return mod


DESIRED = {"pixi.toml": b"[workspace]\n", "pixi.lock": b"version: 6\n"}


class TestDesiredState:
    def test_read_desired_returns_both_files(self, sync, tmp_path):
        config = tmp_path / "config"
        config.mkdir()
        for name, data in DESIRED.items():
            (config / name).write_bytes(data)
        assert sync.read_desired(config) == DESIRED

    def test_read_desired_follows_kubelet_indirection(self, sync, tmp_path):
        """toml+lock must come from the SAME revision dir even if top-level
        symlinks are mid-swap (kubelet ..data pattern)."""
        config = tmp_path / "config"
        rev = config / "..2026_07"
        rev.mkdir(parents=True)
        for name, data in DESIRED.items():
            (rev / name).write_bytes(data)
        os.symlink(rev / "pixi.lock", config / "pixi.lock")
        # pixi.toml symlink deliberately absent — resolution goes through
        # the lock's revision dir
        assert sync.read_desired(config) == DESIRED


class TestDrift:
    def test_in_sync_when_bytes_match(self, sync):
        sync.stage_manifests(sync.LIVE_DIR, DESIRED)
        assert sync.is_in_sync(sync.LIVE_DIR, DESIRED)

    def test_drift_on_any_difference(self, sync):
        sync.stage_manifests(sync.LIVE_DIR, DESIRED)
        changed = dict(DESIRED, **{"pixi.lock": b"version: 6\n# bumped\n"})
        assert not sync.is_in_sync(sync.LIVE_DIR, changed)

    def test_missing_files_count_as_drift(self, sync):
        assert not sync.is_in_sync(sync.LIVE_DIR, DESIRED)

    def test_manual_local_edit_is_drift(self, sync):
        """Sync semantics: local hand-edits get reconciled back to repo
        state (use the pause file for hands-on work)."""
        sync.stage_manifests(sync.LIVE_DIR, DESIRED)
        (sync.LIVE_DIR / "pixi.toml").write_bytes(b"[workspace]\n# local hack\n")
        assert not sync.is_in_sync(sync.LIVE_DIR, DESIRED)


class TestPause:
    def test_pause_file_detected(self, sync):
        assert not sync.is_paused(sync.LIVE_DIR)
        (sync.LIVE_DIR / sync.PAUSE_FILE_NAME).touch()
        assert sync.is_paused(sync.LIVE_DIR)


class TestStageManifests:
    def test_writes_both_files(self, sync, tmp_path):
        target = tmp_path / "t"
        sync.stage_manifests(target, DESIRED)
        for name, data in DESIRED.items():
            assert (target / name).read_bytes() == data

    def test_no_tmp_litter(self, sync, tmp_path):
        target = tmp_path / "t"
        sync.stage_manifests(target, DESIRED)
        assert not list(target.glob(".*.tmp"))

    def test_overwrites_atomically_via_rename(self, sync, tmp_path):
        target = tmp_path / "t"
        sync.stage_manifests(target, DESIRED)
        updated = dict(DESIRED, **{"pixi.lock": b"version: 6\n# v2\n"})
        sync.stage_manifests(target, updated)
        assert (target / "pixi.lock").read_bytes().endswith(b"# v2\n")


class TestLockTakeover:
    def test_fresh_heartbeat_blocks(self, sync):
        heartbeat = {"holder": "other", "pid": 1, "ts": time.time()}
        assert not sync.should_take_over(heartbeat, time.time())

    def test_stale_heartbeat_allows(self, sync):
        heartbeat = {"holder": "other", "pid": 1, "ts": time.time() - 10_000}
        assert sync.should_take_over(heartbeat, time.time())

    def test_garbage_heartbeat_allows(self, sync):
        assert sync.should_take_over(None, time.time())
        assert sync.should_take_over({"junk": True}, time.time())

    def test_heartbeat_roundtrip(self, sync):
        sync.LOCK_DIR.mkdir(parents=True)
        sync.write_heartbeat()
        heartbeat = json.loads(sync.heartbeat_path().read_text())
        assert not sync.should_take_over(heartbeat, time.time())


class TestFrozenHeartbeat:
    def test_changing_heartbeat_never_frozen(self, sync):
        observer = sync.FrozenHeartbeatObserver(threshold=90)
        assert not observer.frozen(100.0, 0)
        assert not observer.frozen(130.0, 200)  # ts changed -> holder alive
        assert not observer.frozen(160.0, 400)

    def test_frozen_heartbeat_detected_after_threshold(self, sync):
        observer = sync.FrozenHeartbeatObserver(threshold=90)
        assert not observer.frozen(100.0, 0)  # first observation
        assert not observer.frozen(100.0, 60)  # frozen, under threshold
        assert observer.frozen(100.0, 91)  # dead holder

    def test_change_resets_the_clock(self, sync):
        observer = sync.FrozenHeartbeatObserver(threshold=90)
        assert not observer.frozen(100.0, 0)
        assert not observer.frozen(130.0, 80)  # changed: clock restarts
        assert not observer.frozen(130.0, 160)  # only 80s frozen
        assert observer.frozen(130.0, 171)


class TestMetrics:
    def test_exposition_renders_all_series(self, sync):
        text = sync.render_metrics()
        assert "pixi_global_sync_in_sync" in text
        assert "pixi_global_sync_paused" in text
        for line in text.strip().splitlines():
            _, value = line.rsplit(" ", 1)
            float(value)  # every sample parses
