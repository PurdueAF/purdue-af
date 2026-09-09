"""af-pod-monitor: exports storage usage metrics for a user's AF pod.

Sidecar in every user pod; Prometheus scrapes :9090 every 5 minutes.

One unreadable directory must not take the others down with it. An
inaccessible /work used to raise out of the loop and kill the process, and
with it the /home utilisation the quota alerts fire on — one pod restarted 275
times that way. Each directory is now read independently and its af_*_dir_ok
gauge says whether the last pass succeeded, so a stale reading is visible
rather than indistinguishable from a fresh one.
"""

import glob
import logging
import os
import subprocess
import sys
import time

from prometheus_client import Gauge, start_http_server

WORK_QUOTA_KB = 104857600  # 100 GB
INTERVAL = 300  # seconds between passes

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


def update_metrics(dir_label: str, directory: str) -> None:
    if dir_label == "work":
        du_output = subprocess.check_output(["du", "-s", directory]).decode("utf-8")
        used, size, util = parse_du_output(du_output)
    else:
        df_output = subprocess.check_output(["df", directory]).decode("utf-8")
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
        for dir_label, directory in directories.items():
            update_directory(dir_label, directory)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
