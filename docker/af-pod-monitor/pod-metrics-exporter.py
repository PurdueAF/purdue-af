"""af-pod-monitor: exports storage usage metrics for a user's AF pod.

Sidecar in every user pod; Prometheus scrapes :9090 every 5 minutes.
"""

import glob
import os
import subprocess
import time

from prometheus_client import Gauge, start_http_server

WORK_QUOTA_KB = 104857600  # 100 GB

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
    metrics[f"{_dl}_dir_last_accessed"] = Gauge(
        f"af_{_dl}_dir_last_accessed",
        f"Last accessed timestamp for {_dl} directory in Analysis Facility",
    )


def discover_username(home_entries):
    """The pod's user is the single /home entry that isn't a system account."""
    skip = {"jovyan", "slurm"}
    return next(d for d in home_entries if d not in skip)


def discover_directories():
    username = discover_username(os.listdir("/home/"))
    return {"home": glob.glob("/home/*")[0], "work": f"/work/users/{username}/"}


def parse_df_output(df_output):
    """Parse `df <dir>` output into (used_kb, size_kb, utilisation)."""
    lines = df_output.strip().split("\n")
    header = lines[0].split()
    data = lines[1].split()

    used = int(data[header.index("Used")])
    size = 0
    util = 0
    for key in ("1K-blocks", "Size"):
        if key in header:
            size = int(data[header.index(key)])
            util = used / size
    return used, size, util


def parse_du_output(du_output, quota_kb=WORK_QUOTA_KB):
    """Parse `du -s <dir>` output into (used_kb, size_kb, utilisation)."""
    used = int(du_output.split()[0])
    return used, quota_kb, used / quota_kb


def update_metrics(dir_label, directory):
    if dir_label == "work":
        du_output = subprocess.check_output(["du", "-s", directory]).decode("utf-8")
        used, size, util = parse_du_output(du_output)
    else:
        df_output = subprocess.check_output(["df", directory]).decode("utf-8")
        used, size, util = parse_df_output(df_output)

    metrics[f"{dir_label}_dir_used"].set(used)
    metrics[f"{dir_label}_dir_size"].set(size)
    metrics[f"{dir_label}_dir_util"].set(util)

    try:
        metrics[f"{dir_label}_dir_last_accessed"].set(os.stat(directory).st_atime)
    except OSError:
        pass


def main():
    directories = discover_directories()
    start_http_server(9090)
    while True:
        for dir_label, directory in directories.items():
            update_metrics(dir_label, directory)
        time.sleep(300)


if __name__ == "__main__":
    main()
