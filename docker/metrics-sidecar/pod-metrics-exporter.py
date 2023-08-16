from prometheus_client import start_http_server, Gauge
import subprocess, time, glob, os

# Create Prometheus metrics for storage metrics
home_dir_used = Gauge('af_home_dir_used_kb', 'Used storage in home directory mounted to an Analysis Facility pod')
home_dir_size = Gauge('af_home_dir_size_kb', 'Total storage in home directory mounted to an Analysis Facility pod')
home_dir_util = Gauge('af_home_dir_util', 'Storage utilization in home directory mounted to an Analysis Facility pod')
home_dir_last_accessed = Gauge('af_home_dir_last_accessed', 'Last accessed timestamp for home directory in Analysis Facility')


def update_metrics():
    directory = glob.glob("/home/*")[0]
    while True:
        # Run the "df" command and get the output
        df_output = subprocess.check_output(["df", directory]).decode('utf-8')
        lines = df_output.strip().split('\n')
        header = lines[0].split()
        data = lines[1].split()

        # Extract relevant information
        used = int(data[header.index('Used')])
        util = 0
        for key in ['1K-blocks', 'Size']:
            if key in header:
                size = int(data[header.index(key)])
                util = used / size

        # Set the metric values
        home_dir_used.set(used)
        home_dir_size.set(size)
        home_dir_util.set(util)

        try:
            stat_info = os.stat(directory)
            last_accessed_time = stat_info.st_atime
            home_dir_last_accessed.set(last_accessed_time)
        except:
            pass
        time.sleep(300)

if __name__ == '__main__':
    # Start the Prometheus HTTP server
    start_http_server(9090)
    # Update the metrics
    update_metrics()
