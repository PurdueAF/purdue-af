"""Sends CPU profiles of the Python process it starts in to Pyroscope. The
runner installs it into each analysis's environment with a .pth file, so
that every script the agent runs there is profiled while it runs."""

import atexit
import os
import sys

started = False


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
            application_name=os.environ.get("PYROSCOPE_APP", "jfc"),
            server_address=server,
            sample_rate=100,
            oncpu=True,
            # Analysis code spends its time in numpy and uproot, outside the GIL.
            gil_only=False,
            tags={
                "analysis": os.environ.get("JFC_ANALYSIS", "unknown"),
                "workspace": os.environ.get("JFC_WORKSPACE", "unknown"),
                "script": os.path.basename(sys.argv[0]) or "python",
            },
        )
        # A short script ends before the next periodic upload.
        atexit.register(pyroscope.shutdown)
    except Exception:
        pass
