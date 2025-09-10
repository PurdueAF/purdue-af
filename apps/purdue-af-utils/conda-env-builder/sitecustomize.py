import os
import sys

try:
    import pyroscope

    srv = os.getenv("PYROSCOPE_SERVER")
    app = os.getenv("PYROSCOPE_APP", "jupyter-server")
    if srv:
        pyroscope.configure(
            application_name=app,
            server_address=srv,
            sample_rate=int(os.getenv("PYROSCOPE_SAMPLE_RATE", "100")),
            oncpu=True,
            gil_only=True,
            detect_subprocesses=False,
            tags={
                "component": "jupyter-server",
                "hub_user": os.getenv("JUPYTERHUB_USER", "unknown"),
            },
        )
        # Optional: write to stderr instead of stdout, or remove entirely
        # sys.__stderr__.write("pyroscope: configured for %s\n" % app)
except Exception as e:
    pass
    # sys.__stderr__.write("pyroscope: init failed: %s\n" % e)
