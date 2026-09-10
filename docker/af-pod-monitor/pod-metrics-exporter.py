"""af-pod-monitor: exports storage usage metrics for a user's AF pod.

Sidecar in every user pod; Prometheus scrapes :9090 every 5 minutes.

One unreadable directory must not take the others down with it. An
inaccessible /work used to raise out of the loop and kill the process, and
with it the /home utilisation the quota alerts fire on — one pod restarted 275
times that way. Each directory is now read independently and its af_*_dir_ok
gauge says whether the last pass succeeded, so a stale reading is visible
rather than indistinguishable from a fresh one.

Failing is not the same as hanging, though: a read on a wedged CephFS/NFS
mount blocks forever in uninterruptible sleep, and an unbounded df would park
the whole loop there while the HTTP server kept serving the last good values —
a session nobody can work in, reported as healthy.

So every pass opens by asking the one question a user would: does this session
still answer? af_session_responsive times a bounded listing of the user's home
directory, which is where a shell or notebook sits, and the heartbeat makes a
wedged pass visible even when every gauge holds its last value. Every command
here runs bounded, because subprocess's own timeout kills the child and then
waits for it — which never returns on a dead mount.
"""

import glob
import logging
import os
import subprocess
import sys
import time

from prometheus_client import Counter, Gauge, start_http_server

WORK_QUOTA_KB = 104857600  # 100 GB
INTERVAL = 300  # seconds between passes
# A session a user would call responsive answers `ls` in well under a second;
# ten is generous enough that load alone never trips it.
PROBE_TIMEOUT_S = 10
# `df` is a statfs call and returns as fast as the mount allows. `du -s` walks
# every file under /work and is legitimately slow on a large tree, so it gets
# far more room — the probe above, not this, is the responsiveness signal.
DF_TIMEOUT_S = 30
DU_TIMEOUT_S = 600

_DIRS = ("home", "work")
metrics = {}
for _dl in _DIRS:
    metrics[f"{_dl}_dir_used"] = Gauge(
        f"af_{_dl}_dir_used_kb",
        f"Used storage in {_dl} directory mounted to an Analysis Facility pod",
    )
    metrics[f"{_dl}_dir_size"] = Gauge(
        f"af_{_dl}_dir_size_kb",
        f"Total storage in {_dl} directory mounted to an Analysis Facility pod",
    )
    metrics[f"{_dl}_dir_util"] = Gauge(
        f"af_{_dl}_dir_util",
        f"Storage utilization in {_dl} directory mounted to an Analysis Facility pod",
    )
    metrics[f"{_dl}_dir_ok"] = Gauge(
        f"af_{_dl}_dir_ok",
        f"1 if the last pass could read the {_dl} directory, 0 otherwise",
    )

session_responsive = Gauge(
    "af_session_responsive",
    "1 if this session answered a bounded listing of its home directory "
    f"within {PROBE_TIMEOUT_S}s, 0 if it timed out or errored",
)
session_probe_seconds = Gauge(
    "af_session_probe_seconds",
    "Time this session took to answer that listing",
)
session_probe_failures = Counter(
    "af_session_probe_failures",
    "Passes in which this session did not answer in time",
)
heartbeat = Gauge(
    "af_pod_monitor_last_pass_timestamp_seconds",
    "Unix time the exporter last completed a pass",
)

log = logging.getLogger("af-pod-monitor")


def discover_username(home_entries: list[str]) -> str:
    """The pod's user is the single /home entry that isn't a system account."""
    skip = {"jovyan", "slurm"}
    return next(d for d in home_entries if d not in skip)


def discover_directories() -> dict[str, str]:
    username = discover_username(os.listdir("/home/"))
    return {"home": glob.glob("/home/*")[0], "work": f"/work/users/{username}/"}


def parse_df_output(df_output: str) -> tuple[int, int, float]:
    """Parse `df <dir>` output into (used_kb, size_kb, utilisation)."""
    lines = df_output.strip().split("\n")
    header = lines[0].split()
    data = lines[1].split()

    used = int(data[header.index("Used")])
    size = 0
    util = 0.0
    for key in ("1K-blocks", "Size"):
        if key in header:
            size = int(data[header.index(key)])
            util = used / size
    return used, size, util


def parse_du_output(
    du_output: str, quota_kb: int = WORK_QUOTA_KB
) -> tuple[int, int, float]:
    """Parse `du -s <dir>` output into (used_kb, size_kb, utilisation)."""
    used = int(du_output.split()[0])
    return used, quota_kb, used / quota_kb


def run_bounded(cmd: list[str], timeout_s: float) -> tuple[bool, str]:
    """Return (ok, stdout). Never blocks past timeout_s: a child stuck on a
    dead mount is killed and abandoned rather than reaped, because SIGKILL is
    not delivered until the syscall returns. subprocess's own timeout waits
    for that child, so it cannot be used here."""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
    except OSError:
        log.exception("could not run %s", cmd)
        return False, ""
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        log.error("%s did not return in %ss", cmd, timeout_s)
        try:
            proc.kill()
            proc.communicate(timeout=1)  # a killable child dies at once
        except Exception:
            pass
        return False, ""
    return proc.returncode == 0, out


def probe_session(home_dir: str) -> bool:
    """The question a user would ask: does this session still answer?"""
    start = time.monotonic()
    ok, _ = run_bounded(["ls", "-la", home_dir], PROBE_TIMEOUT_S)
    session_probe_seconds.set(time.monotonic() - start)
    session_responsive.set(1 if ok else 0)
    if not ok:
        session_probe_failures.inc()
    return ok


def update_metrics(dir_label: str, directory: str) -> None:
    if dir_label == "work":
        ok, du_output = run_bounded(["du", "-s", directory], DU_TIMEOUT_S)
        if not ok:
            raise OSError(f"could not read {directory}")
        used, size, util = parse_du_output(du_output)
    else:
        ok, df_output = run_bounded(["df", directory], DF_TIMEOUT_S)
        if not ok:
            raise OSError(f"could not read {directory}")
        used, size, util = parse_df_output(df_output)

    metrics[f"{dir_label}_dir_used"].set(used)
    metrics[f"{dir_label}_dir_size"].set(size)
    metrics[f"{dir_label}_dir_util"].set(util)


def update_directory(dir_label: str, directory: str) -> bool:
    """One directory's pass. Never raises: a mount the pod cannot read is a
    gap in that directory's metrics, not a reason to stop exporting."""
    try:
        update_metrics(dir_label, directory)
    except Exception:
        log.exception("could not read %s directory (%s)", dir_label, directory)
        metrics[f"{dir_label}_dir_ok"].set(0)
        return False
    metrics[f"{dir_label}_dir_ok"].set(1)
    return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(name)s] %(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    directories = discover_directories()
    start_http_server(9090)
    while True:
        if not probe_session(directories["home"]):
            # Reading usage means touching the mount that just failed to
            # answer; skip the pass rather than hand it to a hung syscall.
            heartbeat.set(time.time())
            time.sleep(INTERVAL)
            continue
        for dir_label, directory in directories.items():
            update_directory(dir_label, directory)
        heartbeat.set(time.time())
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
