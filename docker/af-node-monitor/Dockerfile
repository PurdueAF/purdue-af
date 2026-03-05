FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends fio ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir prometheus_client requests

WORKDIR /opt/af-node-monitor

# Keep copies of the scripts in the image so the same image
# can be used for both orchestrator and workers. The cluster
# may still mount updated versions via ConfigMap.
COPY docker/af-node-monitor/node_healthcheck.py ./node_healthcheck.py
COPY docker/af-node-monitor/mount_worker.py ./mount_worker.py
COPY docker/af-node-monitor/job_runner.py ./job_runner.py

CMD ["python", "node_healthcheck.py"]

