from typing import Any

# JupyterHub injects `c` at exec time; the annotation is for type checkers only.
c: Any

# Ray Client in every session signs in to the Ray Train gateway (apps/ray-train).
c.KubeSpawner.environment.update(
    {
        "RAY_AUTH_MODE": "token",
        # Sent with every call; the gateway asks the Hub whose it is.
        "RAY_AUTH_TOKEN": lambda spawner: spawner.api_token,
    }
)
