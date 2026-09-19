FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends fio ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir prometheus_client==0.22.1 requests==2.32.5 kubernetes==36.0.2

WORKDIR /opt/af-node-monitor

# Fallback copies: the Deployment and DaemonSets run the scripts from a ConfigMap.
COPY docker/af-node-monitor/node_healthcheck.py ./node_healthcheck.py
COPY docker/af-node-monitor/job_runner.py ./job_runner.py
COPY docker/af-node-monitor/probe_agent.py ./probe_agent.py

CMD ["python", "node_healthcheck.py"]

