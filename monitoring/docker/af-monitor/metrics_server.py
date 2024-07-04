from prometheus_client import start_http_server, Gauge
import time

event_rate_per_worker = Gauge('agc_event_rate_per_worker', 'Analysis Grand Challenge: Event rate per worker in kHz')

def update_metrics():
    try:
        with open('/work/projects/purdue-af/agc/metrics/event_rate.txt', 'r') as f:
            event_rate = float(f.read().strip())
        event_rate_per_worker.set(event_rate)
    except Exception as e:
        print(f"Error reading event rate: {e}")
        event_rate_per_worker.set(0)

if __name__ == '__main__':
    start_http_server(8000)
    while True:
        update_metrics()
        time.sleep(120)