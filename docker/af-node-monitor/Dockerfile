FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends fio ca-certificates xrootd-client \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir prometheus_client==0.22.1 kubernetes==36.0.2

WORKDIR /opt/af-node-monitor

# Fallback copies: the Deployment and DaemonSets run the scripts from a ConfigMap.
COPY docker/af-node-monitor/node_healthcheck.py ./node_healthcheck.py
COPY docker/af-node-monitor/job_runner.py ./job_runner.py
COPY docker/af-node-monitor/probe_agent.py ./probe_agent.py
COPY docker/af-node-monitor/xcache_probe.py ./xcache_probe.py

# -B: a __pycache__ here would ship in the image.
RUN fio --version \
    && xrdcp --version \
    && AF_NODE_MONITOR_VERBOSE=1 MOUNT_NAME=smoke CHECK_FILE=smoke \
       python -B -c "import job_runner, node_healthcheck, xcache_probe; print('ok')"

CMD ["python", "node_healthcheck.py"]

