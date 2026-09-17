"""Tests for apps/af-utils/pixi-global-sync/sync-global-env.py — the daemon
that keeps /work/pixi/global in sync with the repo. Exercises the pure
logic (drift detection, pause, atomic manifest staging, lock takeover) and
the reconcile loop on tmp filesystems, with pixi and check-env faked; the
pixi install itself is validated upstream by ci-pixi-global.yml and
post-checked in production by check-env."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request

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
    mod.CONFIG_DIR = tmp_path / "config"
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

    def test_counters(self, sync):
        sync.metric_set("in_sync", 1)
        sync.metric_inc("syncs_total")
        sync.metric_inc("syncs_total", 2)
        text = sync.render_metrics()
        assert "pixi_global_sync_in_sync 1.0\n" in text
        assert "pixi_global_sync_syncs_total 3.0\n" in text


class TestHttpEndpoints:
    @pytest.fixture()
    def base_url(self, sync):
        sync.METRICS_PORT = 0
        server = sync.start_metrics_server()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()
        server.server_close()

    @staticmethod
    def get(url):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()

    def test_metrics(self, sync, base_url):
        status, body = self.get(f"{base_url}/metrics")
        assert status == 200
        assert body == sync.render_metrics()

    def test_healthz_follows_the_loop_heartbeat(self, sync, base_url):
        assert self.get(f"{base_url}/healthz") == (500, "stalled\n")
        sync.metric_set("loop_heartbeat_timestamp_seconds", time.time())
        assert self.get(f"{base_url}/healthz") == (200, "ok\n")

    def test_unknown_path(self, base_url):
        assert self.get(f"{base_url}/nope")[0] == 404


def fake_clock(sync, monkeypatch, step=0.0):
    """time.sleep advances a fake monotonic clock by the slept amount."""
    clock = {"mono": 0.0, "slept": []}

    def sleep(seconds):
        clock["slept"].append(seconds)
        clock["mono"] += seconds

    monkeypatch.setattr(
        sync,
        "time",
        types.SimpleNamespace(
            time=time.time, sleep=sleep, monotonic=lambda: clock["mono"]
        ),
    )
    return clock


def write_foreign_heartbeat(sync, **payload):
    sync.LOCK_DIR.mkdir(parents=True)
    sync.heartbeat_path().write_text(json.dumps(payload))


class TestAcquireLock:
    def test_free_lock_is_taken(self, sync):
        sync.acquire_lock()
        assert json.loads(sync.heartbeat_path().read_text())["pid"] == os.getpid()

    def test_stale_holder_is_replaced(self, sync, monkeypatch):
        clock = fake_clock(sync, monkeypatch)
        write_foreign_heartbeat(sync, holder="old", pid=1, ts=time.time() - 10_000)
        sync.acquire_lock()
        assert clock["slept"] == []
        assert json.loads(sync.heartbeat_path().read_text())["pid"] == os.getpid()

    def test_unreadable_heartbeat_is_replaced(self, sync, monkeypatch):
        fake_clock(sync, monkeypatch)
        sync.LOCK_DIR.mkdir(parents=True)
        sync.heartbeat_path().write_text("{torn")
        sync.acquire_lock()
        assert json.loads(sync.heartbeat_path().read_text())["pid"] == os.getpid()

    def test_frozen_holder_is_replaced_after_waiting(self, sync, monkeypatch):
        """A fresh-but-unchanging heartbeat: wait, then take over once it has
        been frozen past LOCK_FROZEN_SECONDS."""
        clock = fake_clock(sync, monkeypatch)
        write_foreign_heartbeat(sync, holder="dead", pid=1, ts=time.time())
        sync.acquire_lock()
        assert clock["slept"] == [30] * 4  # 120 s > 90 s threshold
        assert json.loads(sync.heartbeat_path().read_text())["pid"] == os.getpid()

    def test_release_removes_the_lock(self, sync):
        sync.acquire_lock()
        sync.release_lock()
        assert not sync.LOCK_DIR.exists()
        sync.release_lock()  # already gone: fine


class FastEvent(threading.Event):
    """Heartbeat interval shrunk from 30 s to 10 ms."""

    def wait(self, timeout=None):
        return super().wait(0.01 if timeout else timeout)


class TestRunWithHeartbeat:
    def test_returns_combined_output(self, sync):
        proc = sync.run_with_heartbeat(
            [
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr)",
            ],
            timeout=30,
        )
        assert proc.returncode == 0
        assert "out" in proc.stdout and "err" in proc.stdout
        assert sync._current_child["proc"] is None

    def test_passes_cwd_through(self, sync, tmp_path):
        proc = sync.run_with_heartbeat(
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            timeout=30,
            cwd=tmp_path,
        )
        assert os.path.samefile(proc.stdout.strip(), tmp_path)

    def test_timeout_kills_the_child(self, sync):
        with pytest.raises(subprocess.TimeoutExpired):
            sync.run_with_heartbeat(
                [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2
            )
        assert sync._current_child["proc"] is None

    def test_long_runs_keep_both_heartbeats_fresh(self, sync, monkeypatch):
        monkeypatch.setattr(
            sync,
            "threading",
            types.SimpleNamespace(Event=FastEvent, Thread=threading.Thread),
        )
        sync.LOCK_DIR.mkdir(parents=True)
        sync.run_with_heartbeat(
            [sys.executable, "-c", "import time; time.sleep(0.3)"], 30
        )
        assert sync.METRICS["loop_heartbeat_timestamp_seconds"] > 0
        assert sync.heartbeat_path().is_file()

    def test_heartbeat_write_failure_is_tolerated(self, sync, monkeypatch):
        monkeypatch.setattr(
            sync,
            "threading",
            types.SimpleNamespace(Event=FastEvent, Thread=threading.Thread),
        )
        # no LOCK_DIR: every heartbeat write raises OSError
        proc = sync.run_with_heartbeat(
            [sys.executable, "-c", "import time; time.sleep(0.1)"], 30
        )
        assert proc.returncode == 0

    def test_refuses_to_start_once_stopping(self, sync):
        sync.STOP.set()
        with pytest.raises(RuntimeError, match="stopping"):
            sync.run_with_heartbeat([sys.executable, "-c", ""], 30)


def failing(sync, calls, stdout="boom"):
    def run(cmd, timeout, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout=stdout)

    return run


class TestPixiInstall:
    def test_success_runs_once(self, sync, monkeypatch, tmp_path):
        calls = []

        def run(cmd, timeout, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout="")

        monkeypatch.setattr(sync, "run_with_heartbeat", run)
        sync.pixi_install(tmp_path)
        assert calls == [
            (
                [sync.PIXI_BIN, "install", "--locked", "--environment", sync.ENV_NAME],
                {"cwd": tmp_path},
            )
        ]

    def test_retries_with_backoff_then_fails(self, sync, monkeypatch, tmp_path):
        calls, waits = [], []
        monkeypatch.setattr(sync, "run_with_heartbeat", failing(sync, calls))
        monkeypatch.setattr(sync.STOP, "wait", lambda t: waits.append(t) or False)
        with pytest.raises(RuntimeError, match="after retries"):
            sync.pixi_install(tmp_path)
        assert len(calls) == 3
        assert waits == [60, 120]

    def test_stop_during_backoff_aborts(self, sync, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(sync, "run_with_heartbeat", failing(sync, calls))
        monkeypatch.setattr(sync.STOP, "wait", lambda t: True)
        with pytest.raises(RuntimeError, match="stopping"):
            sync.pixi_install(tmp_path)
        assert len(calls) == 1


def site_packages(sync, env_dir):
    site = (
        env_dir
        / ".pixi"
        / "envs"
        / sync.ENV_NAME
        / "lib"
        / "python3.12"
        / "site-packages"
    )
    site.mkdir(parents=True)
    return site


class TestEnvPrefix:
    def test_purge_removes_only_hollow_dist_infos(self, sync, tmp_path):
        site = site_packages(sync, tmp_path)
        (site / "awkward-2.0.dist-info").mkdir()
        (site / "awkward-2.0.dist-info" / "METADATA").write_text("Version: 2.0")
        (site / "awkward-1.0.dist-info").mkdir()  # interrupted install
        (site / "awkward").mkdir()
        assert sync.purge_incomplete_dist_infos(tmp_path) == 1
        assert sorted(p.name for p in site.iterdir()) == [
            "awkward",
            "awkward-2.0.dist-info",
        ]

    def test_purge_without_an_env(self, sync, tmp_path):
        assert sync.purge_incomplete_dist_infos(tmp_path) == 0

    def test_wipe_prefix(self, sync, tmp_path):
        site_packages(sync, tmp_path)
        (tmp_path / "pixi.toml").write_text("")
        sync.wipe_env_prefix(tmp_path)
        assert not (tmp_path / ".pixi" / "envs" / sync.ENV_NAME).exists()
        assert (tmp_path / "pixi.toml").exists()
        sync.wipe_env_prefix(tmp_path)  # nothing left: no error


class TestValidateEnv:
    @pytest.fixture()
    def env_python(self, sync, tmp_path):
        python = tmp_path / ".pixi" / "envs" / sync.ENV_NAME / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("")
        return python

    def test_missing_interpreter_fails_without_running(
        self, sync, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(sync, "run_with_heartbeat", pytest.fail)
        assert sync.validate_env(tmp_path) is False

    @pytest.mark.parametrize("stdout", ["checked 3\nall 3 imported\n\n", ""])
    def test_passes_on_zero_exit(self, sync, monkeypatch, tmp_path, env_python, stdout):
        calls = []

        def run(cmd, timeout, **kwargs):
            calls.append((cmd, timeout))
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout)

        monkeypatch.setattr(sync, "run_with_heartbeat", run)
        assert sync.validate_env(tmp_path) is True
        assert calls == [
            (
                [
                    env_python,
                    sync.CONFIG_DIR / "check-env.py",
                    "--manifest",
                    tmp_path / "pixi.toml",
                    "--env",
                    sync.ENV_NAME,
                ],
                sync.VALIDATE_TIMEOUT,
            )
        ]

    def test_fails_on_nonzero_exit(self, sync, monkeypatch, tmp_path, env_python):
        monkeypatch.setattr(sync, "run_with_heartbeat", failing(sync, []))
        assert sync.validate_env(tmp_path) is False


class TestHelpers:
    def test_short_hash(self, sync):
        assert sync.short_hash(None) == "absent"
        assert len(sync.short_hash(b"x")) == 8

    def test_live_lock_bytes(self, sync):
        assert sync._live_lock_bytes() is None
        sync.stage_manifests(sync.LIVE_DIR, DESIRED)
        assert sync._live_lock_bytes() == DESIRED["pixi.lock"]


@pytest.fixture()
def pipeline(sync, monkeypatch):
    """Desired state on the config mount; pixi/check-env replaced by
    recorders. `results` feeds validate_env's answers in order."""
    sync.CONFIG_DIR.mkdir()
    for name, data in DESIRED.items():
        (sync.CONFIG_DIR / name).write_bytes(data)
    state = {"events": [], "results": []}

    monkeypatch.setattr(
        sync, "pixi_install", lambda d: state["events"].append("install")
    )
    monkeypatch.setattr(
        sync, "wipe_env_prefix", lambda d: state["events"].append("wipe")
    )
    monkeypatch.setattr(
        sync, "purge_incomplete_dist_infos", lambda d: state["events"].append("purge")
    )

    def validate(d):
        state["events"].append("validate")
        return state["results"].pop(0) if state["results"] else True

    monkeypatch.setattr(sync, "validate_env", validate)
    return state


class TestReconcile:
    def test_in_sync_and_healthy_is_a_no_op(self, sync, pipeline):
        sync.stage_manifests(sync.LIVE_DIR, DESIRED)
        assert sync.reconcile() is True
        assert pipeline["events"] == []
        assert sync.METRICS["in_sync"] == 1.0

    def test_drift_is_synced(self, sync, pipeline):
        assert sync.reconcile() is True
        assert pipeline["events"] == ["install", "purge", "validate"]
        assert sync.is_in_sync(sync.LIVE_DIR, DESIRED)
        assert sync.METRICS["in_sync"] == 1.0
        assert sync.METRICS["syncs_total"] == 1.0
        assert sync.METRICS["last_success_timestamp_seconds"] > 0

    def test_failed_validation_wipes_and_retries_once(self, sync, pipeline):
        pipeline["results"] = [False, True]
        assert sync.reconcile() is True
        assert pipeline["events"] == [
            "install",
            "purge",
            "validate",
            "wipe",
            "install",
            "purge",
            "validate",
        ]
        assert sync.METRICS["env_healthy"] == 1.0

    def test_second_validation_failure_marks_unhealthy(self, sync, pipeline):
        pipeline["results"] = [False, False]
        assert sync.reconcile() is False
        assert sync.METRICS["env_healthy"] == 0.0
        assert sync.METRICS["sync_failures_total"] == 1.0
        assert sync._last_failure["ts"] > 0
        # manifests were staged before the install, so the drift check alone
        # would call this in sync
        assert sync.is_in_sync(sync.LIVE_DIR, DESIRED)

    def test_cooldown_after_a_failure(self, sync, pipeline):
        sync._last_failure["ts"] = time.time()
        assert sync.reconcile() is False
        assert pipeline["events"] == []
        assert sync.reconcile(force=True) is True  # force ignores the cooldown
        assert "install" in pipeline["events"]

    def test_unhealthy_env_in_sync_is_healed_with_a_wipe(self, sync, pipeline):
        sync.stage_manifests(sync.LIVE_DIR, DESIRED)
        sync.metric_set("env_healthy", 0.0)
        assert sync.reconcile() is True
        assert pipeline["events"][0] == "wipe"
        assert sync.METRICS["env_healthy"] == 1.0

    def test_install_error_is_counted_not_raised(self, sync, pipeline, monkeypatch):
        def broken(d):
            raise RuntimeError("pixi install failed after retries")

        monkeypatch.setattr(sync, "pixi_install", broken)
        assert sync.reconcile() is False
        assert sync.METRICS["sync_failures_total"] == 1.0
        assert sync.METRICS["last_sync_duration_seconds"] >= 0

    def test_paused_env_is_left_alone(self, sync, pipeline, caplog):
        (sync.LIVE_DIR / sync.PAUSE_FILE_NAME).touch()
        with caplog.at_level("INFO", logger="pixi-global-sync"):
            assert sync.reconcile() is True
            assert sync.reconcile() is True
        assert pipeline["events"] == []
        assert sync.METRICS["paused"] == 1.0
        assert sync.METRICS["in_sync"] == 0.0
        assert [r.message for r in caplog.records].count(
            "paused"
        ) == 1  # transitions only

        (sync.LIVE_DIR / sync.PAUSE_FILE_NAME).unlink()
        with caplog.at_level("INFO", logger="pixi-global-sync"):
            assert sync.reconcile() is True
        assert sync.METRICS["paused"] == 0.0
        assert any("resumed" in r.message for r in caplog.records)
        assert "install" in pipeline["events"]

    def test_sigterm_mid_install_does_not_retry_or_wipe(self, sync, monkeypatch):
        """Regression: a child killed by the SIGTERM handler used to read as a
        failed install — 60 s backoff, retries, then a prefix wipe — so the
        daemon outlived its grace period and was SIGKILLed holding the lock."""
        sync.CONFIG_DIR.mkdir()
        for name, data in DESIRED.items():
            (sync.CONFIG_DIR / name).write_bytes(data)
        spawned, wiped = [], []

        class TerminatedChild:
            returncode = -signal.SIGTERM

            def __init__(self, cmd, **kwargs):
                spawned.append(cmd)

            def communicate(self, timeout=None):
                sync.STOP.set()  # the handler fires while pixi runs
                return "", None

        monkeypatch.setattr(sync.subprocess, "Popen", TerminatedChild)
        monkeypatch.setattr(sync, "wipe_env_prefix", wiped.append)
        clock = fake_clock(sync, monkeypatch)
        monkeypatch.setattr(sync.STOP, "wait", pytest.fail)
        assert sync.reconcile() is False
        assert len(spawned) == 1
        assert clock["slept"] == []
        assert wiped == []


class TestDeepVerify:
    def test_skipped_while_paused_or_before_first_sync(self, sync, pipeline):
        sync.deep_verify()
        sync.stage_manifests(sync.LIVE_DIR, DESIRED)
        (sync.LIVE_DIR / sync.PAUSE_FILE_NAME).touch()
        sync.deep_verify()
        assert pipeline["events"] == []

    def test_healthy_env_is_not_touched(self, sync, pipeline):
        sync.stage_manifests(sync.LIVE_DIR, DESIRED)
        sync.deep_verify()
        assert pipeline["events"] == ["validate"]
        assert sync.METRICS["env_healthy"] == 1.0

    def test_broken_env_forces_a_resync(self, sync, pipeline):
        sync.stage_manifests(sync.LIVE_DIR, DESIRED)
        sync._last_failure["ts"] = time.time()  # force bypasses the cooldown
        pipeline["results"] = [False]
        sync.deep_verify()
        assert pipeline["events"] == [
            "validate",
            "wipe",
            "install",
            "purge",
            "validate",
        ]
        assert sync.METRICS["env_healthy"] == 1.0


class TestMain:
    @pytest.fixture()
    def daemon(self, sync, monkeypatch, tmp_path):
        """main() with the lock, metrics server and signals stubbed; the
        loop runs until a reconcile call sets STOP."""
        state = {"events": [], "handlers": {}, "cycles": []}
        sync.POLL_SECONDS = 0
        monkeypatch.setenv("TMPDIR", str(tmp_path / "cache" / "tmp"))
        monkeypatch.setattr(
            sync, "acquire_lock", lambda: state["events"].append("lock")
        )
        monkeypatch.setattr(
            sync, "release_lock", lambda: state["events"].append("unlock")
        )
        monkeypatch.setattr(sync, "start_metrics_server", lambda: None)
        monkeypatch.setattr(
            sync, "deep_verify", lambda: state["events"].append("verify")
        )
        monkeypatch.setattr(
            sync.signal,
            "signal",
            lambda sig, handler: state["handlers"].__setitem__(sig, handler),
        )
        monkeypatch.setattr(sync.logging, "basicConfig", lambda **kw: None)

        def reconcile():
            outcome = state["cycles"].pop(0)
            state["events"].append("reconcile")
            if outcome == "raise":
                raise RuntimeError("configmap missing")
            if outcome == "stop":
                sync.STOP.set()

        monkeypatch.setattr(sync, "reconcile", reconcile)
        return state

    def test_loop_survives_a_failed_cycle_and_releases_the_lock(
        self, sync, daemon, tmp_path, caplog
    ):
        daemon["cycles"] = ["raise", "stop"]
        assert sync.main() == 0
        assert daemon["events"] == [
            "lock",
            "reconcile",
            "reconcile",
            "verify",
            "unlock",
        ]
        assert (tmp_path / "cache" / "tmp").is_dir()
        assert sync.CACHE_DIR.is_dir()
        assert sync.METRICS["loop_heartbeat_timestamp_seconds"] > 0
        assert "reconcile cycle failed" in caplog.text
        # acquire_lock is stubbed, so the lock dir is missing: warned, not fatal
        assert "heartbeat write failed" in caplog.text

    def test_verify_runs_only_every_verify_seconds(self, sync, daemon):
        sync.LOCK_DIR.mkdir(parents=True)
        daemon["cycles"] = ["ok", "ok", "stop"]
        sync.main()
        assert daemon["events"].count("verify") == 1

    def test_signal_handler_stops_the_loop_and_kills_the_child(self, sync, daemon):
        daemon["cycles"] = ["stop"]
        sync.main()
        assert set(daemon["handlers"]) == {signal.SIGTERM, signal.SIGINT}
        sync.STOP.clear()
        child = types.SimpleNamespace(terminated=False)
        child.terminate = lambda: setattr(child, "terminated", True)
        sync._current_child["proc"] = child
        daemon["handlers"][signal.SIGTERM](signal.SIGTERM, None)
        assert sync.STOP.is_set()
        assert child.terminated
        sync._current_child["proc"] = None
        daemon["handlers"][signal.SIGINT](signal.SIGINT, None)  # no child: fine
