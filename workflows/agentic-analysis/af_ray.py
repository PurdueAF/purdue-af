"""Gives an analysis's scripts the facility's GPUs through Ray (apps/ray-train),
running the environment they run in. The runner installs it into each
analysis's environment; the task pod's workload account token says whose Ray
cluster it is."""

import os
import sys
from typing import Any

from af_dask import account_token

# The Ray Train gateway's Jobs API, in the task pods' namespace.
ADDRESS = "http://ray-train-gateway:8265"


def client(**settings: object) -> Any:
    """A Ray JobSubmissionClient whose jobs each run on a Ray cluster of their own.
    Each keyword is one of the clusters' settings, its name without `af-` and
    with `_` for `-`: `max_workers=4` is `af-max-workers: "4"`. The clusters run
    this environment unless `env` names another."""
    from ray.job_submission import JobSubmissionClient

    # Explicit: Ray reads RAY_AUTH_TOKEN once per process, maybe before this call.
    headers = {
        "Authorization": f"Bearer {account_token()}",
        "af-env": os.path.realpath(sys.prefix),
    }
    headers |= {
        f"af-{key.replace('_', '-')}": str(value) for key, value in settings.items()
    }
    return JobSubmissionClient(ADDRESS, headers=headers)
