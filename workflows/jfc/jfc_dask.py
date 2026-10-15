"""Gives an analysis's scripts a Dask cluster on the facility's Dask Gateway
that runs the environment they run in. The runner installs it into each
analysis's environment; the task pod's DASK_GATEWAY__* variables and
JUPYTERHUB_API_TOKEN name the gateway and whose clusters they are."""

import fcntl
import os
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path
from typing import Any

try:
    from distributed import WorkerPlugin
except ImportError:
    # The runner imports this module without Dask to install it.
    WorkerPlugin = object

# The gateway requires PATH; the rest make workers profile and verify XCache as the scripts do.
PASSED = (
    "PATH",
    "PYROSCOPE_SERVER",
    "PYROSCOPE_APP",
    "JFC_ANALYSIS",
    "JFC_WORKSPACE",
    "X509_CERT_DIR",
)
# Seconds between handing the workers the mounted proxy, which af-x509-secrets renews.
RENEW = 1800


class Proxy(WorkerPlugin):
    """Puts a grid proxy into a file of the worker's own, for XRootD."""

    name = "jfc-proxy"

    def __init__(self, data: bytes) -> None:
        self.data = data

    def setup(self, worker: Any) -> None:
        path = Path(tempfile.gettempdir()) / "jfc-x509up"
        draft = path.with_name(f"{path.name}.new")
        with open(
            os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb"
        ) as file:
            file.write(self.data)
        os.replace(draft, path)
        os.environ["X509_USER_PROXY"] = str(path)


def client(workers: int = 50, cores: float = 1, memory: float = 4) -> Any:
    """A dask.distributed Client of the analysis's cluster, which grows to at
    most `workers` workers while there is work and shrinks to none without.
    A cluster this call starts has workers of `cores` cores and `memory` GiB.
    The analysis's scripts share one cluster; it ends an hour after the last
    of them."""
    from dask_gateway import Gateway

    gateway = Gateway()
    prefix = Path(sys.prefix)
    # A pixi environment is <project>/.pixi/envs/<name>.
    project = str(prefix.parents[2])
    # The gateway's one cluster per user does not hold for requests made at once.
    with open(prefix / "jfc_dask.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cluster = _running(gateway, project, cores, memory) or gateway.new_cluster(
            pixi_project=project,
            pixi_env=prefix.name,
            worker_cores=cores,
            worker_memory=memory,
            env={name: os.environ[name] for name in PASSED if name in os.environ},
            shutdown_on_close=False,
        )
    cluster.adapt(minimum=0, maximum=workers)
    dask = cluster.get_client()
    if os.environ.get("X509_USER_PROXY"):
        _hand_proxy(dask, Path(os.environ["X509_USER_PROXY"]))
    return dask


def _running(gateway: Any, project: str, cores: float, memory: float) -> Any:
    """The cluster running `project`, connected; None if there is none."""
    reports = gateway.list_clusters()
    for report in reports:
        if report.options.get("pixi_project") == project:
            size = (
                report.options.get("worker_cores"),
                report.options.get("worker_memory"),
            )
            if size != (cores, memory):
                warnings.warn(
                    f"{report.name} keeps its workers of {size[0]} cores and "
                    f"{size[1]} GiB until it ends.",
                    stacklevel=3,
                )
            return gateway.connect(report.name)
    if reports:
        raise RuntimeError(
            f"Dask Gateway runs one cluster per user, and {reports[0].name} is "
            "running: wait until it ends, and do not stop it."
        )
    return None


def _hand_proxy(dask: Any, proxy: Path) -> None:
    """Gives every worker of the cluster, those yet to start included, the
    proxy at `proxy`, again every RENEW seconds while `dask` is open."""

    def hand() -> None:
        if proxy.is_file():
            dask.register_plugin(Proxy(proxy.read_bytes()), name=Proxy.name)
        else:
            print(f"jfc_dask: no proxy at {proxy} to hand the workers", file=sys.stderr)

    def renew() -> None:
        while dask.status not in ("closing", "closed"):
            time.sleep(RENEW)
            try:
                hand()
            except Exception as failed:
                print(
                    f"jfc_dask: the workers' proxy is not renewed: {failed}",
                    file=sys.stderr,
                )

    hand()
    threading.Thread(target=renew, daemon=True).start()
