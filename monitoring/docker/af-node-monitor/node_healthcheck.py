from prometheus_client import start_http_server, Gauge
import time
import subprocess

# Define the Gauge metric with labels for mount name and mount path
try:
    mount_valid = Gauge('af_node_mount_valid', 'Storage mount health', ['mount_name', 'mount_path'])
except Exception as e:
    print(f"Error defining Gauge metric: {e}")

# Define the mount paths and their expected checksums
# Key: mount name (e.g., "/depot/"), Value: (file_to_check, expected_checksum)
mounts = {
    "/depot/": ("/depot/cms/purdue-af/validate-mount.txt", "13dede34ee8dc7e5b70c9cd06ac15467"),
    "/work/": ("/work/projects/purdue-af/validate-mount.txt", "f4cb7f2740ba3e87edfbda6c70fa94c2"),
    "eos": ("/eos/purdue/store/user/dkondrat/test.root", "18864b0de8ae5a6a8d3b459a7999b431"),
    "cvmfs": ("/cvmfs/cms.cern.ch/cmsset_default.sh", "aaf910393256dbbac56d42973324deb7")
}

def check_if_directory_exists(path_tuple):
    filename, expected_checksum = path_tuple
    try:
        # Run md5sum with a timeout of 3 seconds
        proc = subprocess.Popen(["/usr/bin/md5sum", filename],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True)
        try:
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            print(f"Timeout occurred while checking file {filename}")
            return False

        # If md5sum returned nonzero, it's an error
        if proc.returncode != 0:
            print(f"md5sum failed for {filename}. Stderr: {stderr.strip()}")
            return False

        # Parse the checksum from md5sum output
        parts = stdout.strip().split()
        if len(parts) < 1:
            print(f"Could not parse md5sum output for {filename}: {stdout}")
            return False

        checksum = parts[0]
        if checksum != expected_checksum:
            print(f"Wrong checksum for {filename}. Expected: {expected_checksum}, Got: {checksum}")
            return False

        return True
    except Exception as e:
        print(f"Error checking file {filename}: {e}")
        return False

def update_metrics():
    for m_name, m_path in mounts.items():
        result = check_if_directory_exists(m_path)
        mount_valid.labels(mount_name=m_name, mount_path=m_path[0]).set(1 if result else 0)

if __name__ == '__main__':
    # Start the HTTP server to expose the metrics
    start_http_server(8000)
    while True:
        update_metrics()
        time.sleep(120)