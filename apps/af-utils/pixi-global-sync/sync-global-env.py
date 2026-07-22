#!/usr/bin/env python3
"""Automated hands for the shared global pixi env: keeps /work/pixi/global
in sync with the repo's pixi/global/{pixi.toml,pixi.lock}.

Deliberately does EXACTLY what an admin does by hand — update the two
manifest files, run `pixi install --locked`, confirm the env imports —
nothing else. /work/pixi/global stays a plain pixi project directory
(no symlinks, no build copies, no versioned dirs); the only addition on
the share is a persistent package cache at /work/pixi/.cache.

Safety model:
  - Correctness of the LOCK is guaranteed upstream: ci-pixi solves and
    import-smokes every lock change inside the AF image before it can
    merge — nothing unvalidated ever reaches this daemon.
  - The in-place `pixi install --locked` is transactional per-package and,
    with a warm cache on the same filesystem, mostly hardlink swaps: the
    update window is short and its failure modes are infra-only (retried
    with backoff; a failed attempt leaves pixi to converge on retry).
  - After every install (and periodically), check-env.py import-smokes the
    live env; failure ⇒ env_healthy=0 (alerts) + forced re-sync.
  - Drift detection is byte-comparison of pixi.lock (mounted desired vs
    live) — no extra state files. Manual edits in the live dir therefore
    count as drift and are reverted within POLL_SECONDS; for hands-on
    work, `touch /work/pixi/global/.sync-pause` stops the daemon touching
    the env until the file is removed (surfaced in metrics).

Desired state arrives via a ConfigMap mounted at CONFIG_DIR — kubelet
refreshes the mount in place (~1 min after Flux applies), so the daemon
needs no restart. Metrics + /healthz on METRICS_PORT. Runs inside the
purdue-af image (same pixi + EL8 userland as user sessions). Stdlib only.
"""

import hashlib
import http.server
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("pixi-global-sync")

# ── configuration (env-overridable) ──────────────────────────────────────
WORK_ROOT = Path(os.environ.get("WORK_ROOT", "/work/pixi"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
PIXI_BIN = os.environ.get("PIXI_BIN", "/opt/pixi/bin/pixi")
ENV_NAME = os.environ.get("ENV_NAME", "default")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
VERIFY_SECONDS = int(os.environ.get("VERIFY_SECONDS", "21600"))  # 6 h
BUILD_TIMEOUT = int(os.environ.get("BUILD_TIMEOUT", "5400"))  # 90 min
VALIDATE_TIMEOUT = int(os.environ.get("VALIDATE_TIMEOUT", "1800"))
FAIL_COOLDOWN = int(os.environ.get("FAIL_COOLDOWN", "1800"))
LOCK_STALE_SECONDS = int(os.environ.get("LOCK_STALE_SECONDS", "600"))
# a holder whose heartbeat value stops CHANGING is dead (live ones refresh
# every 30 s) — take over after this many seconds of a frozen heartbeat
LOCK_FROZEN_SECONDS = int(os.environ.get("LOCK_FROZEN_SECONDS", "90"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9099"))
# CI gate: only sync a lock whose introducing commit has ALL GitHub checks
# green. Fail-closed (API errors block syncing, never break the live env).
REQUIRE_CI_CHECKS = os.environ.get("REQUIRE_CI_CHECKS", "1") == "1"
GITHUB_REPO = os.environ.get("GITHUB_REPO", "PurdueAF/purdue-af")
# gate on the whole env dir: toml-only commits must be blessed too
GITHUB_GATE_PATH = os.environ.get("GITHUB_GATE_PATH", "pixi/global")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

LIVE_DIR = WORK_ROOT / "global"  # plain pixi project dir, as it always was
CACHE_DIR = WORK_ROOT / ".cache"
LOCK_DIR = CACHE_DIR / ".sync-lock"
PAUSE_FILE_NAME = ".sync-pause"

# ── metrics (hand-rolled exposition; stdlib only) ────────────────────────
METRICS = {
    "in_sync": 0.0,  # 1 = live pixi.lock matches the repo's
    "paused": 0.0,  # 1 = .sync-pause present, daemon hands-off
    "env_healthy": 1.0,  # 0 after a failed verify, until healed
    "ci_gate_ok": 1.0,  # 0 = desired lock blocked (checks pending/failed)
    "last_success_timestamp_seconds": 0.0,
    "last_attempt_timestamp_seconds": 0.0,
    "syncs_total": 0.0,
    "sync_failures_total": 0.0,
    "last_sync_duration_seconds": 0.0,
    "loop_heartbeat_timestamp_seconds": 0.0,
}
_metrics_lock = threading.Lock()


def metric_set(name, value):
    with _metrics_lock:
        METRICS[name] = float(value)


def metric_inc(name, delta=1.0):
    with _metrics_lock:
        METRICS[name] += delta


def render_metrics():
    with _metrics_lock:
        lines = [
            f"pixi_global_sync_{name} {value}"
            for name, value in sorted(METRICS.items())
        ]
    return "\n".join(lines) + "\n"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib API)
        if self.path == "/metrics":
            body = render_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
        elif self.path == "/healthz":
            # alive = the loop (or a long install's heartbeat) ticked recently
            fresh = time.time() - METRICS["loop_heartbeat_timestamp_seconds"] < max(
                POLL_SECONDS * 5, 600
            )
            body = b"ok\n" if fresh else b"stalled\n"
            self.send_response(200 if fresh else 500)
            self.send_header("Content-Type", "text/plain")
        else:
            body = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet access log
        pass


def start_metrics_server():
    server = http.server.ThreadingHTTPServer(("", METRICS_PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ── desired state & drift ────────────────────────────────────────────────
def read_desired(config_dir=None):
    """→ {filename: bytes}. Reads through the kubelet ..data indirection so
    toml+lock always come from the SAME atomic ConfigMap revision."""
    config_dir = Path(config_dir) if config_dir else CONFIG_DIR
    revision_dir = (config_dir / "pixi.lock").resolve().parent
    return {
        name: (revision_dir / name).read_bytes() for name in ("pixi.toml", "pixi.lock")
    }


def is_in_sync(live_dir, desired_files):
    """Drift check = byte equality of the two manifests. No state files."""
    live_dir = Path(live_dir)
    try:
        return all(
            (live_dir / name).read_bytes() == data
            for name, data in desired_files.items()
        )
    except OSError:
        return False


def is_paused(live_dir=None):
    live_dir = Path(live_dir) if live_dir else LIVE_DIR
    return (live_dir / PAUSE_FILE_NAME).exists()


def stage_manifests(target_dir, files):
    """Write pixi.toml/pixi.lock into target_dir, each atomically
    (tmp + rename) so a concurrent reader never sees a torn file."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        tmp = target_dir / f".{name}.tmp"
        tmp.write_bytes(data)
        tmp.replace(target_dir / name)


# ── CI gate ──────────────────────────────────────────────────────────────
OK_CONCLUSIONS = {"success", "neutral", "skipped"}
BAD_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "stale"}


def aggregate_check_runs(runs):
    """GitHub check-runs → ('success'|'pending'|'failure', detail)."""
    if not runs:
        return "pending", "no check runs reported yet"
    bad = [r["name"] for r in runs if r.get("conclusion") in BAD_CONCLUSIONS]
    if bad:
        return "failure", "failed checks: " + ", ".join(sorted(bad))
    unfinished = [
        r["name"]
        for r in runs
        if r.get("status") != "completed" or r.get("conclusion") not in OK_CONCLUSIONS
    ]
    if unfinished:
        return "pending", "awaiting: " + ", ".join(sorted(unfinished)[:5])
    return "success", f"{len(runs)} checks green"


class CiGate:
    """Answers: is the desired lock content blessed by CI on main?

    Uses conditional requests (ETag) so the steady state costs zero API
    rate limit; blessed shas are cached forever. `fetch` is injectable
    for tests: fetch(url) -> (status, body_bytes)."""

    def __init__(self, fetch=None):
        self.fetch = fetch or self._fetch_urllib
        self._etags = {}  # url -> (etag, cached_body)
        self._blessed = set()  # shas fully verified green
        self._content_ok = {}  # sha -> bool (blob matches desired)
        self._last_logged = None

    def _fetch_urllib(self, url):
        import urllib.error
        import urllib.request

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "pixi-global-sync",
        }
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        etag, cached = self._etags.get(url, (None, None))
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as resp:
                body = resp.read()
                if resp.headers.get("ETag"):
                    self._etags[url] = (resp.headers["ETag"], body)
                return resp.status, body
        except urllib.error.HTTPError as err:
            if err.code == 304 and cached is not None:
                return 200, cached
            return err.code, err.read() if err.fp else b""

    def _json(self, url):
        status, body = self.fetch(url)
        if status != 200:
            raise RuntimeError(f"GitHub API {status} for {url}")
        return json.loads(body)

    def evaluate(self, desired_files):
        """desired_files = {"pixi.toml": bytes, "pixi.lock": bytes}
        → (allowed: bool, reason: str). Never raises."""
        try:
            commits = self._json(
                f"https://api.github.com/repos/{GITHUB_REPO}/commits"
                f"?path={GITHUB_GATE_PATH}&sha=main&per_page=1"
            )
            if not commits:
                return False, "no commit found for the env path"
            sha = commits[0]["sha"]

            if sha not in self._content_ok:
                match = True
                for name, desired_bytes in desired_files.items():
                    status, blob = self.fetch(
                        f"https://raw.githubusercontent.com/{GITHUB_REPO}/{sha}"
                        f"/{GITHUB_GATE_PATH}/{name}"
                    )
                    if status != 200:
                        return False, f"cannot fetch {name} at {sha[:8]} ({status})"
                    match = match and blob == desired_bytes
                self._content_ok[sha] = match
            if not self._content_ok[sha]:
                return False, (
                    f"desired manifests do not match env commit {sha[:8]} "
                    "(Flux catching up?)"
                )
            if sha in self._blessed:
                return True, f"commit {sha[:8]} already verified"

            runs = self._json(
                f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}"
                "/check-runs?per_page=100"
            ).get("check_runs", [])
            verdict, detail = aggregate_check_runs(runs)
            if verdict == "success":
                self._blessed.add(sha)
                return True, f"commit {sha[:8]}: {detail}"
            return False, f"commit {sha[:8]}: {detail}"
        except Exception as err:  # fail-closed, but never crash the loop
            return False, f"CI gate error (fail-closed): {err}"

    def log_once(self, allowed, reason):
        key = (allowed, reason.split(" (")[0])
        if key != self._last_logged:
            (log.info if allowed else log.warning)(
                "CI gate: %s — %s", "PASS" if allowed else "BLOCKED", reason
            )
            self._last_logged = key


# ── singleton lock (NFS-safe: mkdir is atomic; heartbeat allows takeover) ─
def heartbeat_path():
    return LOCK_DIR / "heartbeat.json"


def write_heartbeat():
    payload = {"holder": socket.gethostname(), "pid": os.getpid(), "ts": time.time()}
    tmp = LOCK_DIR / f".hb-{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload))
    tmp.replace(heartbeat_path())


def should_take_over(heartbeat, now, stale_seconds=None):
    stale_seconds = LOCK_STALE_SECONDS if stale_seconds is None else stale_seconds
    if heartbeat is None:
        return True
    try:
        return (now - float(heartbeat["ts"])) > stale_seconds
    except (KeyError, TypeError, ValueError):
        return True


class FrozenHeartbeatObserver:
    """A LIVE lock holder rewrites its heartbeat every 30 s, so the ts field
    keeps changing. If we observe the SAME ts for longer than `threshold`,
    the holder is dead (e.g. SIGKILLed mid-install before it could release)
    — safe to take over long before the absolute-age staleness kicks in."""

    def __init__(self, threshold):
        self.threshold = threshold
        self._seen = None  # (heartbeat_ts, first_observed_monotonic)

    def frozen(self, heartbeat_ts, now_monotonic):
        if self._seen is None or self._seen[0] != heartbeat_ts:
            self._seen = (heartbeat_ts, now_monotonic)
            return False
        return (now_monotonic - self._seen[1]) > self.threshold


def acquire_lock():
    observer = FrozenHeartbeatObserver(LOCK_FROZEN_SECONDS)
    while True:
        try:
            LOCK_DIR.mkdir(parents=True)
            write_heartbeat()
            log.info("lock acquired")
            return
        except FileExistsError:
            try:
                heartbeat = json.loads(heartbeat_path().read_text())
            except (OSError, ValueError):
                heartbeat = None
            now = time.time()
            if should_take_over(heartbeat, now):
                log.warning("taking over stale lock (heartbeat=%s)", heartbeat)
                write_heartbeat()
                return
            if observer.frozen(heartbeat.get("ts"), time.monotonic()):
                log.warning(
                    "lock holder %s (pid %s) is dead — heartbeat frozen for "
                    ">%ss; taking over",
                    heartbeat.get("holder"),
                    heartbeat.get("pid"),
                    LOCK_FROZEN_SECONDS,
                )
                write_heartbeat()
                return
            age = now - float(heartbeat.get("ts", now))
            log.info(
                "lock held by %s (pid %s), heartbeat %.0fs old — waiting "
                "(takeover on frozen>%ss or age>%ss)",
                heartbeat.get("holder"),
                heartbeat.get("pid"),
                age,
                LOCK_FROZEN_SECONDS,
                LOCK_STALE_SECONDS,
            )
            time.sleep(30)


def release_lock():
    shutil.rmtree(LOCK_DIR, ignore_errors=True)


# ── the actual work: what an admin would type, automated ─────────────────
_current_child = {"proc": None}  # terminated by the SIGTERM handler


def run_with_heartbeat(cmd, timeout, **kwargs):
    """Run a long subprocess while keeping liveness + lock heartbeats
    fresh (pixi install can take tens of minutes). The child is registered
    so the SIGTERM handler can kill it — otherwise the daemon would block
    past the pod grace period, get SIGKILLed, and leave the lock behind."""
    log.info("run: %s", " ".join(map(str, cmd)))
    stop = threading.Event()

    def _beat():
        while not stop.wait(30):
            metric_set("loop_heartbeat_timestamp_seconds", time.time())
            try:
                write_heartbeat()
            except OSError:
                pass

    beater = threading.Thread(target=_beat, daemon=True)
    beater.start()
    proc = subprocess.Popen(
        list(map(str, cmd)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )
    _current_child["proc"] = proc
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, _ = proc.communicate()
        raise
    finally:
        _current_child["proc"] = None
        stop.set()
        beater.join(timeout=5)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout=stdout)


def pixi_install(env_dir):
    attempts = 3
    for attempt in range(1, attempts + 1):
        proc = run_with_heartbeat(
            [PIXI_BIN, "install", "--locked", "--environment", ENV_NAME],
            timeout=BUILD_TIMEOUT,
            cwd=env_dir,
        )
        if proc.returncode == 0:
            return
        log.error(
            "pixi install failed (attempt %d/%d):\n%s",
            attempt,
            attempts,
            proc.stdout[-4000:],
        )
        if attempt < attempts:
            time.sleep(60 * attempt)
    raise RuntimeError("pixi install failed after retries")


def purge_incomplete_dist_infos(env_dir):
    """Remove hollow *.dist-info dirs left by interrupted PyPI installs.

    importlib.metadata discovers them by directory name and can return
    Version=None when METADATA is missing — that breaks packages like
    `vector` which parse `awkward`'s version at import time. Pixi install
    may add the new complete dist-info without removing the hollow one.
    """
    env_dir = Path(env_dir)
    site_root = env_dir / ".pixi" / "envs" / ENV_NAME / "lib"
    removed = 0
    for site in site_root.glob("python*/site-packages"):
        for dist in site.glob("*.dist-info"):
            if not (dist / "METADATA").is_file():
                shutil.rmtree(dist, ignore_errors=True)
                removed += 1
    if removed:
        log.warning("purged %d incomplete *.dist-info dirs under %s", removed, env_dir)
    return removed


def wipe_env_prefix(env_dir):
    """Delete the installed env prefix so the next pixi install is clean.
    Package cache on the share keeps this from being a full cold download."""
    prefix = Path(env_dir) / ".pixi" / "envs" / ENV_NAME
    if prefix.exists():
        log.warning("wiping env prefix for clean reinstall: %s", prefix)
        shutil.rmtree(prefix)


def validate_env(env_dir):
    """Import-smoke every declared dependency (check-env.py) with the
    env's own interpreter."""
    env_dir = Path(env_dir)
    python = env_dir / ".pixi" / "envs" / ENV_NAME / "bin" / "python"
    if not python.is_file():
        log.error("check-env FAILED: env python missing at %s", python)
        return False
    proc = run_with_heartbeat(
        [
            python,
            CONFIG_DIR / "check-env.py",
            "--manifest",
            env_dir / "pixi.toml",
            "--env",
            ENV_NAME,
        ],
        timeout=VALIDATE_TIMEOUT,
    )
    if proc.returncode == 0:
        lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
        log.info("check-env: %s", lines[-1] if lines else "ok")
    else:
        log.error("check-env FAILED:\n%s", proc.stdout[-6000:])
    return proc.returncode == 0


# ── reconcile ────────────────────────────────────────────────────────────
_last_failure = {"ts": 0.0}
_was_paused = {"value": False}
_ci_gate = CiGate()


def short_hash(data):
    return hashlib.sha256(data).hexdigest()[:8] if data is not None else "absent"


def _live_lock_bytes():
    try:
        return (LIVE_DIR / "pixi.lock").read_bytes()
    except OSError:
        return None


def reconcile(force=False):
    desired = read_desired()
    paused = is_paused()
    if paused != _was_paused["value"]:  # log transitions only, not every cycle
        log.info("paused" if paused else "resumed (.sync-pause removed)")
        _was_paused["value"] = paused
    if paused:
        metric_set("paused", 1.0)
        metric_set("in_sync", 1.0 if is_in_sync(LIVE_DIR, desired) else 0.0)
        return True
    metric_set("paused", 0.0)

    in_sync = is_in_sync(LIVE_DIR, desired)
    metric_set("in_sync", 1.0 if in_sync else 0.0)
    # Manifests are staged BEFORE pixi install, so a failed install leaves
    # in_sync=1 with a broken env. Keep forcing until env_healthy recovers
    # (bounded by FAIL_COOLDOWN below) — otherwise we'd sit unhealthy until
    # the next 6 h deep_verify.
    if in_sync and not force and METRICS["env_healthy"] >= 0.5:
        return True
    if not force and time.time() - _last_failure["ts"] < FAIL_COOLDOWN:
        return False

    if REQUIRE_CI_CHECKS:
        allowed, reason = _ci_gate.evaluate(desired)
        _ci_gate.log_once(allowed, reason)
        metric_set("ci_gate_ok", 1.0 if allowed else 0.0)
        if not allowed:
            return False  # re-evaluated next cycle (ETag-cached, cheap)

    log.info(
        "%s: live lock %s -> desired %s; syncing",
        "forced re-sync" if force else "drift detected",
        short_hash(_live_lock_bytes()),
        short_hash(desired["pixi.lock"]),
    )
    metric_set("last_attempt_timestamp_seconds", time.time())
    metric_inc("syncs_total")
    started = time.time()
    try:
        stage_manifests(LIVE_DIR, desired)
        # Healing a previously-broken live env: incremental install can leave
        # hollow dist-info / half-linked trees. Wipe once so cache hardlinks
        # rebuild a coherent prefix.
        if METRICS["env_healthy"] < 0.5:
            wipe_env_prefix(LIVE_DIR)
        pixi_install(LIVE_DIR)
        purge_incomplete_dist_infos(LIVE_DIR)
        if not validate_env(LIVE_DIR):
            log.warning(
                "post-install validation failed — wiping prefix and retrying once"
            )
            wipe_env_prefix(LIVE_DIR)
            pixi_install(LIVE_DIR)
            purge_incomplete_dist_infos(LIVE_DIR)
            if not validate_env(LIVE_DIR):
                metric_set("env_healthy", 0.0)
                raise RuntimeError(
                    "env failed post-install validation (CI validated this lock, "
                    "so this is likely infra: disk/NFS/network). Will retry."
                )
    except Exception:
        metric_inc("sync_failures_total")
        _last_failure["ts"] = time.time()
        log.exception("sync failed")
        return False
    finally:
        metric_set("last_sync_duration_seconds", time.time() - started)

    metric_set("in_sync", 1.0)
    metric_set("env_healthy", 1.0)
    metric_set("last_success_timestamp_seconds", time.time())
    log.info("in sync (%.0f s)", time.time() - started)
    return True


def deep_verify():
    """Periodic import-smoke of the live env; failure alerts and forces a
    re-sync (self-heal). Skipped while paused."""
    if is_paused() or not (LIVE_DIR / "pixi.toml").exists():
        return
    healthy = validate_env(LIVE_DIR)
    metric_set("env_healthy", 1.0 if healthy else 0.0)
    if not healthy:
        log.error("live env failed deep verification — forcing re-sync")
        reconcile(force=True)


# ── main ─────────────────────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    log.info(
        "starting: env=%s poll=%ss verify=%ss cache=%s pixi=%s",
        LIVE_DIR,
        POLL_SECONDS,
        VERIFY_SECONDS,
        CACHE_DIR,
        PIXI_BIN,
    )
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # /cache is an emptyDir that starts empty; pixi's PyPI phase creates
    # tempfile locks under TMPDIR and fails hard if the parent is missing.
    if tmp := os.environ.get("TMPDIR"):
        Path(tmp).mkdir(parents=True, exist_ok=True)
    acquire_lock()
    stop = threading.Event()

    def _terminate(signum, frame):
        log.info("signal %s — stopping (killing any in-flight install)", signum)
        stop.set()
        child = _current_child["proc"]
        if child is not None:
            child.terminate()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    start_metrics_server()

    last_verify = 0.0
    try:
        while not stop.is_set():
            metric_set("loop_heartbeat_timestamp_seconds", time.time())
            try:
                write_heartbeat()
            except OSError as err:
                log.warning("heartbeat write failed: %s", err)
            try:
                reconcile()
                if time.time() - last_verify > VERIFY_SECONDS:
                    last_verify = time.time()
                    deep_verify()
            except Exception:
                log.exception("reconcile cycle failed")
            stop.wait(POLL_SECONDS)
    finally:
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
