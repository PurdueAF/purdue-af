from typing import Any

# JupyterHub injects `c` at exec time; the annotation is for type checkers only.
c: Any

# The Ray Jobs CLI in every session talks to the Ray Train gateway (apps/ray-train).
c.KubeSpawner.environment.update(
    {
        # Read by `ray job` only; RAY_ADDRESS would also redirect a local ray.init().
        "RAY_API_SERVER_ADDRESS": "http://ray-train-gateway.cms.svc.cluster.local:8265",
        "RAY_AUTH_MODE": "token",
        # Sent as the bearer token; the gateway asks the Hub whose it is.
        "RAY_AUTH_TOKEN": lambda spawner: spawner.api_token,
    }
)
