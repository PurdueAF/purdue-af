"""Helm waits 5 minutes for Alloy's DaemonSet; a roll that outlasts it fails, rolls back and loops."""

import math

import yaml
from common import REPO

HELM_WAIT_SECONDS = 300
# Readiness takes about 40 s, and the DaemonSet controller starts the next batch about 10 s later.
BATCH_SECONDS = 60


def test_a_full_roll_fits_inside_helms_wait():
    alloy = yaml.safe_load((REPO / "apps/monitoring/alloy/values.yaml").read_text())
    batch = alloy["controller"]["updateStrategy"]["rollingUpdate"]["maxUnavailable"]
    prometheus = yaml.safe_load(
        (REPO / "apps/monitoring/prometheus/values.yaml").read_text()
    )
    jobs = prometheus["serverFiles"]["prometheus.yml"]["scrape_configs"]
    (job,) = [j for j in jobs if j["job_name"] == "node-exporter"]
    nodes = sum(len(config["targets"]) for config in job["static_configs"])
    assert math.ceil(nodes / batch) * BATCH_SECONDS < HELM_WAIT_SECONDS
