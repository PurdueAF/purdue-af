"""Sends CPU profiles of the Python process it starts in to Pyroscope. The
runner installs it into each analysis's environment with a .pth file, so
that every script the agent runs there is profiled while it runs."""

import atexit
import hashlib
import os
import sys

started = False


def command() -> dict[str, str]:
    """The tags of the command of the agent's this process runs in. A Dask
    worker has none: it serves many, and each task brings them."""
    names = {"session": "AF_SESSION", "agent": "AF_AGENT", "call": "AF_CALL"}
    return {tag: os.environ[name] for tag, name in names.items() if name in os.environ}


def _script() -> str:
    # multiprocessing starts its children, Dask's workers among them, as `python -c`.
    if "--multiprocessing-fork" in sys.argv:
        return "spawned"
    return os.path.basename(sys.argv[0]) or "python"


def _lock() -> str:
    """The beginning of the hash of the lock file of this environment,
    <project>/.pixi/envs/<name>."""
    try:
        with open(os.path.join(sys.prefix, "../../../pixi.lock"), "rb") as lock:
            return hashlib.sha256(lock.read()).hexdigest()[:12]
    except OSError:
        return "unknown"


def start() -> None:
    """Profiles this process if PYROSCOPE_SERVER names a server; a failure
    leaves the process as it was."""
    global started
    server = os.environ.get("PYROSCOPE_SERVER")
    if started or not server:
        return
    started = True
    try:
        import pyroscope

        pyroscope.configure(
            application_name=os.environ.get("PYROSCOPE_APP", "agentic-analysis"),
            server_address=server,
            sample_rate=100,
            oncpu=True,
            # Analysis code spends its time in numpy and uproot, outside the GIL.
            gil_only=False,
            tags={
                "analysis": os.environ.get("AF_ANALYSIS", "unknown"),
                "workspace": os.environ.get("AF_WORKSPACE", "unknown"),
                "script": _script(),
                "lock": _lock(),
                **command(),
            },
        )
        # A short script ends before the next periodic upload.
        atexit.register(pyroscope.shutdown)
    except Exception:
        pass
