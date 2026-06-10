"""Test helpers for the exporter suite (flat scripts, not packages)."""

import os

from common import REPO, load_script


def pod_exporter():
    return load_script(
        REPO / "docker" / "af-pod-monitor" / "pod-metrics-exporter.py",
        "pod_metrics_exporter",
    )


def job_runner():
    # job_runner requires env at import and silences stdio unless verbose.
    os.environ.setdefault("AF_NODE_MONITOR_VERBOSE", "1")
    os.environ.setdefault("MOUNT_NAME", "/depot/")
    os.environ.setdefault("CHECK_FILE", "/depot/validate.txt")
    import job_runner as module

    return module
