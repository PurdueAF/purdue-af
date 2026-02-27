import json
import subprocess
import time

from prometheus_client import Gauge, start_http_server

# Define the Gauge metric with labels for mount name and mount path
try:
    mount_valid = Gauge(
        "af_node_mount_valid", "Storage mount health", ["mount_name", "mount_path"]
    )
    mount_ping_ms = Gauge(
        "af_node_mount_ping_ms",
        "Storage mount ping time in milliseconds",
        ["mount_name", "mount_path"],
    )
    mount_data_rate_gbps = Gauge(
        "af_node_mount_data_rate_gbps",
        "Storage mount sequential read throughput in Gbps",
        ["mount_name", "mount_path"],
    )
    mount_metadata_latency_ms = Gauge(
        "af_node_mount_metadata_latency_ms",
        "Storage mount metadata latency in milliseconds (ls)",
        ["mount_name", "mount_path"],
    )
except Exception as e:
    print(f"Error defining Gauge metric: {e}")

# Define the mount paths and their expected checksums
# Key: mount name (e.g., "/depot/"), Value: (file_to_check, expected_checksum)
mounts = {
    "/depot/": (
        "/depot/cms/purdue-af/validate-mount.txt",
        "13dede34ee8dc7e5b70c9cd06ac15467",
    ),
    "/work/": (
        "/work/projects/purdue-af/validate-mount.txt",
        "f4cb7f2740ba3e87edfbda6c70fa94c2",
    ),
    "eos": (
        "/eos/purdue/store/user/dkondrat/test.root",
        "18864b0de8ae5a6a8d3b459a7999b431",
    ),
    "cvmfs": (
        "/cvmfs/cms.cern.ch/SITECONF/T2_US_Purdue/Purdue-Hadoop/JobConfig/site-local-config.xml",
        "3b570d80272b7188c13cef51e58b7151",
    ),
}

# Mounts to run throughput checks on, and their probe file paths
throughput_probes = {
    "/depot/": "/depot/cms/purdue-af/.storage-monitoring-probe-1gb",
    "/work/": "/work/projects/purdue-af/.storage-monitoring-probe-1gb",
    "eos": "/eos/purdue/store/user/dkondrat/.storage-monitoring-probe-1gb",
}

# Directories to run metadata (ls) latency checks on
metadata_probes = {
    "/depot/": "/depot/cms/",
    "/work/": "/work/users/",
    "eos": "/eos/purdue/store/user/",
    "cvmfs": "/cvmfs/cms.cern.ch/",
}

METADATA_TIMEOUT_S = 10

# How often to run the heavy fio check, in iterations of the main loop (120s each)
# Default: every 15 iterations = every 30 minutes
FIO_INTERVAL = 15
fio_counter = 0


def check_if_directory_exists(path_tuple):
    filename, expected_checksum = path_tuple
    start_time = time.time()

    try:
        # Run md5sum with a timeout of 3 seconds
        proc = subprocess.Popen(
            ["/usr/bin/md5sum", filename],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            print(f"Timeout occurred while checking file {filename}")
            return False, 3000  # Return 3000ms for timeout

        # Calculate elapsed time in milliseconds
        elapsed_ms = (time.time() - start_time) * 1000

        # If md5sum returned nonzero, it's an error
        if proc.returncode != 0:
            print(f"md5sum failed for {filename}. Stderr: {stderr.strip()}")
            return False, elapsed_ms

        # Parse the checksum from md5sum output
        parts = stdout.strip().split()
        if len(parts) < 1:
            print(f"Could not parse md5sum output for {filename}: {stdout}")
            return False, elapsed_ms

        checksum = parts[0]
        if checksum != expected_checksum:
            print(
                f"Wrong checksum for {filename}. Expected: {expected_checksum}, Got: {checksum}"
            )
            return False, elapsed_ms

        return True, elapsed_ms
    except Exception as e:
        elapsed_ms = (time.time() - start_time) * 1000
        print(f"Error checking file {filename}: {e}")
        return False, elapsed_ms


def check_read_throughput(mount_name, probe_file):
    print(f"Running fio throughput check for {mount_name} using {probe_file}")
    try:
        result = subprocess.run(
            [
                "fio",
                "--name=read_test",
                f"--filename={probe_file}",
                "--rw=read",
                "--bs=1M",
                "--size=1G",
                "--numjobs=1",
                "--readonly",
                "--output-format=json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        data = json.loads(result.stdout)
        bw_bytes = data["jobs"][0]["read"]["bw_bytes"]
        gbps = bw_bytes / 1e9
        print(f"Throughput for {mount_name}: {gbps:.2f} Gbps")
        return gbps
    except Exception as e:
        print(f"fio error for {mount_name} ({probe_file}): {e}")
        return None


def check_metadata_latency(mount_name, probe_dir):
    print(f"Running metadata latency check for {mount_name} in {probe_dir}")
    start_time = time.time()
    try:
        result = subprocess.run(
            ["ls", "-la", probe_dir],
            capture_output=True,
            text=True,
            timeout=METADATA_TIMEOUT_S,
        )
        elapsed_ms = (time.time() - start_time) * 1000
        if result.returncode != 0:
            print(f"ls failed for {probe_dir}: {result.stderr.strip()}")
            return None
        print(f"Metadata latency for {mount_name}: {elapsed_ms:.1f} ms")
        return elapsed_ms
    except subprocess.TimeoutExpired:
        elapsed_ms = (time.time() - start_time) * 1000
        print(f"Metadata check timed out for {mount_name} ({probe_dir})")
        return elapsed_ms
    except Exception as e:
        print(f"Metadata check error for {mount_name} ({probe_dir}): {e}")
        return None


def update_metrics():
    global fio_counter

    # Lightweight health checks (every iteration)
    for m_name, m_path in mounts.items():
        result, ping_time = check_if_directory_exists(m_path)
        mount_valid.labels(mount_name=m_name, mount_path=m_path[0]).set(
            1 if result else 0
        )
        mount_ping_ms.labels(mount_name=m_name, mount_path=m_path[0]).set(ping_time)

    # Metadata latency checks (every iteration, lightweight)
    for m_name, probe_dir in metadata_probes.items():
        latency_ms = check_metadata_latency(m_name, probe_dir)
        if latency_ms is not None:
            mount_metadata_latency_ms.labels(
                mount_name=m_name, mount_path=probe_dir
            ).set(latency_ms)

    # Heavy throughput checks (every FIO_INTERVAL iterations)
    if fio_counter % FIO_INTERVAL == 0:
        for m_name, probe_file in throughput_probes.items():
            gbps = check_read_throughput(m_name, probe_file)
            if gbps is not None:
                mount_data_rate_gbps.labels(
                    mount_name=m_name, mount_path=probe_file
                ).set(gbps)

    fio_counter += 1


if __name__ == "__main__":
    # Start the HTTP server to expose the metrics
    start_http_server(8000)
    while True:
        update_metrics()
        time.sleep(120)
