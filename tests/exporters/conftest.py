"""Path setup for exporter tests — the exporters are flat scripts, not packages."""

import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO / "docker" / "af-node-monitor"))
sys.path.insert(0, str(REPO / "apps" / "monitoring" / "af-monitoring"))


def load_script(path, module_name):
    """Import a script with a non-importable filename (dashes)."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


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
