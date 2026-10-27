"""Gives an analysis's scripts a Dask cluster on the facility's Dask Gateway
that runs the environment they run in. The runner installs it into each
analysis's environment; the task pod's DASK_GATEWAY__* variables name the
gateway, and its workload account's token whose clusters they are."""

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
    from dask_gateway.auth import GatewayAuth
    from distributed import WorkerPlugin
except ImportError:
    # The runner imports this module without Dask to install it.
    GatewayAuth = WorkerPlugin = object

# The variable naming the file that holds the workload account's token, which af_ray reads too.
TOKEN_FILE = "AF_ACCOUNT_TOKEN_FILE"

# The gateway requires PATH; the rest make workers profile and verify XCache as the scripts do.
PASSED = (
    "PATH",
    "PYROSCOPE_SERVER",
    "PYROSCOPE_APP",
    "AF_ANALYSIS",
    "AF_WORKSPACE",
    "X509_CERT_DIR",
)
# Seconds between handing the workers the mounted proxy, which af-x509-secrets renews.
RENEW = 1800


def account_token() -> str:
    return Path(os.environ[TOKEN_FILE]).read_text().strip()


class AccountAuth(GatewayAuth):
    """The workload account's token, as the gateway asks for it: on every request."""

    def pre_request(self, resp: Any) -> tuple[dict[str, str], None]:
        return {"Authorization": f"Bearer {account_token()}"}, None


class Proxy(WorkerPlugin):
    """Puts a grid proxy into a file of the worker's own, for XRootD."""

    name = "af-proxy"

    def __init__(self, data: bytes) -> None:
        self.data = data

    def setup(self, worker: Any) -> None:
        path = Path(tempfile.gettempdir()) / "af-x509up"
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

    gateway = Gateway(auth=AccountAuth())
    prefix = Path(sys.prefix)
    # A pixi environment is <project>/.pixi/envs/<name>.
    project = str(prefix.parents[2])
    # The gateway's one cluster per user does not hold for requests made at once.
    with open(prefix / "af_dask.lock", "w") as lock:
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
            print(f"af_dask: no proxy at {proxy} to hand the workers", file=sys.stderr)

    def renew() -> None:
        while dask.status not in ("closing", "closed"):
            time.sleep(RENEW)
            try:
                hand()
            except Exception as failed:
                print(
                    f"af_dask: the workers' proxy is not renewed: {failed}",
                    file=sys.stderr,
                )

    hand()
    threading.Thread(target=renew, daemon=True).start()
