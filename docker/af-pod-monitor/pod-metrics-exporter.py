import glob
import os
import subprocess
import time

from prometheus_client import Gauge, start_http_server

# Create Prometheus metrics for storage metrics

user_dirs = [d for d in os.listdir("/home/")]
skip = ["jovyan", "slurm"]
username = [d for d in user_dirs if d not in skip][0]

directories = {"home": glob.glob("/home/*")[0], "work": f"/work/users/{username}/"}

metrics = {}
for dl in directories.keys():
    metrics[f"{dl}_dir_used"] = Gauge(
        f"af_{dl}_dir_used_kb",
        f"Used storage in {dl} directory mounted to an Analysis Facility pod",
    )
    metrics[f"{dl}_dir_size"] = Gauge(
        f"af_{dl}_dir_size_kb",
        f"Total storage in {dl} directory mounted to an Analysis Facility pod",
    )
    metrics[f"{dl}_dir_util"] = Gauge(
        f"af_{dl}_dir_util",
        f"Storage utilization in {dl} directory mounted to an Analysis Facility pod",
    )
    metrics[f"{dl}_dir_last_accessed"] = Gauge(
        f"af_{dl}_dir_last_accessed",
        f"Last accessed timestamp for {dl} directory in Analysis Facility",
    )


def update_metrics(dir_label):
    directory = directories[dir_label]

    if dir_label == "work":
        du_output = subprocess.check_output(["du", "-s", directory]).decode("utf-8")
        used = int(du_output.split()[0])
        size = 104857600  # 100 GB
        util = used / size
    else:
        # Run the "df" command and get the output
        df_output = subprocess.check_output(["df", directory]).decode("utf-8")
        lines = df_output.strip().split("\n")
        header = lines[0].split()
        data = lines[1].split()

        # Extract relevant information
        used = int(data[header.index("Used")])
        util = 0
        for key in ["1K-blocks", "Size"]:
            if key in header:
                size = int(data[header.index(key)])
                util = used / size

    # Set the metric values
    metrics[f"{dl}_dir_used"].set(used)
    metrics[f"{dl}_dir_size"].set(size)
    metrics[f"{dl}_dir_util"].set(util)

    try:
        stat_info = os.stat(directory)
        last_accessed_time = stat_info.st_atime
        metrics[f"{dl}_dir_last_accessed"].set(last_accessed_time)
    except:
        pass


if __name__ == "__main__":
    # Start the Prometheus HTTP server
    start_http_server(9090)
    # Update the metrics
    while True:
        for dl in directories.keys():
            update_metrics(dl)
        time.sleep(300)
