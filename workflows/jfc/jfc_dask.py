"""Gives an analysis's scripts a Dask cluster on the facility's Dask Gateway
that runs the environment they run in. The runner installs it into each
analysis's environment; the task pod's DASK_GATEWAY__* variables and
JUPYTERHUB_API_TOKEN name the gateway and whose clusters they are."""

import fcntl
import os
import sys
from pathlib import Path
from typing import Any

# The gateway requires PATH and puts the environment's bin/ in front; with the
# rest, the cluster's processes send profiles as the scripts do (jfc_profiling).
PASSED = ("PATH", "PYROSCOPE_SERVER", "PYROSCOPE_APP", "JFC_ANALYSIS", "JFC_WORKSPACE")


def client(workers: int = 50) -> Any:
    """A dask.distributed Client of the analysis's cluster, which grows to at
    most `workers` one-core workers while there is work and shrinks to none
    without. The analysis's scripts share one cluster; it ends an hour after
    the last of them."""
    from dask_gateway import Gateway

    gateway = Gateway()
    prefix = Path(sys.prefix)
    # A pixi environment is <project>/.pixi/envs/<name>.
    project = str(prefix.parents[2])
    # The gateway's one cluster per user does not hold for requests made at once.
    with open(prefix / "jfc_dask.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cluster = _running(gateway, project) or gateway.new_cluster(
            pixi_project=project,
            pixi_env=prefix.name,
            env={name: os.environ[name] for name in PASSED if name in os.environ},
            shutdown_on_close=False,
        )
    cluster.adapt(minimum=0, maximum=workers)
    return cluster.get_client()


def _running(gateway: Any, project: str) -> Any:
    """The cluster running `project`, connected; None if there is none."""
    reports = gateway.list_clusters()
    for report in reports:
        if report.options.get("pixi_project") == project:
            return gateway.connect(report.name)
    if reports:
        raise RuntimeError(
            f"Dask Gateway runs one cluster per user, and {reports[0].name} is "
            "running: wait until it ends, and do not stop it."
        )
    return None
