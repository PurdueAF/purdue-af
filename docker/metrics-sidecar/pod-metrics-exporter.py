from prometheus_client import start_http_server, Gauge
import subprocess, time, glob

# Create Prometheus metrics for storage metrics
home_dir_used = Gauge('af_home_dir_used_kb', 'Used storage in home directory mounted to an Analysis Facility pod')
home_dir_avail = Gauge('af_home_dir_avail_kb', 'Available storage in home directory mounted to an Analysis Facility pod')
home_dir_util = Gauge('af_home_dir_util', 'Storage utilization in home directory mounted to an Analysis Facility pod')

def update_metrics():
    while True:
        # Run the "df" command and get the output
        df_output = subprocess.check_output(["df", glob.glob("/home/*")[0]]).decode('utf-8')
        lines = df_output.strip().split('\n')
        
        # Parse the header and data lines of the "df" output
        header = lines[0].split()
        data = lines[1].split()

        # Extract relevant information
        used = int(data[header.index('Used')])
        avail = int(data[header.index('Available')])
        util = used / avail

        # Set the metric values
        home_dir_used.set(used)
        home_dir_avail.set(avail)
        home_dir_util.set(util)

        time.sleep(300)

if __name__ == '__main__':
    # Start the Prometheus HTTP server
    start_http_server(9090)
    # Update the metrics
    update_metrics()
