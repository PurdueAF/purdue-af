from prometheus_client import start_http_server, Gauge
import os
import time
import subprocess
import threading

# Define the Gauge metric with labels for mount name and mount path
try:
    mount_valid = Gauge('af_node_mount_valid', 'Storage mount health', ['mount_name', 'mount_path'])
except Exception as e:
    print(f"Error defining Gauge metric: {e}")

# Define the mount paths to check
mounts = {
    "/depot/": "/depot/cms/hmm/",
    "/work/": "/work/users/",
    "eos": "/eos/purdue/store/mc/",
    "cvmfs": "/cvmfs/cms.cern.ch/"
}

def watchdog(proc, timeout, result):
    proc.wait(timeout)
    if proc.poll() is None:
        proc.terminate()
        time.sleep(1)
        if proc.poll() is None:
            proc.kill()
    result['completed'] = proc.poll() is not None

def check_if_directory_exists(path):
    try:
        # Start the subprocess
        proc = subprocess.Popen(['test', '-d', path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = {'completed': False}
        
        # Start the watchdog thread
        thread = threading.Thread(target=watchdog, args=(proc, 3, result))
        thread.start()
        thread.join()
        
        if not result['completed']:
            print(f"Timeout occurred while checking directory {path}")
            return False
        
        return proc.returncode == 0
    except Exception as e:
        print(f"Error checking directory {path}: {e}")
        return False

def update_metrics():
    for m_name, m_path in mounts.items():
        result = check_if_directory_exists(m_path)
        mount_valid.labels(mount_name=m_name, mount_path=m_path).set(1 if result else 0)

if __name__ == '__main__':
    # Start the HTTP server to expose the metrics
    start_http_server(8000)
    while True:
        update_metrics()
        time.sleep(120)