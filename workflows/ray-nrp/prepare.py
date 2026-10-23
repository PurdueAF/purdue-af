"""Writes the training inputs to NRP S3 with Ray Data: a synthetic regression
set in the columns train.py reads. Runs on the AF (prepare-job.yaml)."""

import os
from urllib.parse import urlparse

import numpy as np
import pyarrow.fs
import ray

BUCKET = os.environ["S3_BUCKET"]
INPUTS = f"{BUCKET}/ray-nrp/inputs"
FEATURES = 16
ROWS = 4_000_000


def s3() -> pyarrow.fs.S3FileSystem:
    endpoint = urlparse(os.environ["AWS_ENDPOINT_URL"])
    # Ceph ignores the region; naming one keeps pyarrow from asking AWS for it.
    return pyarrow.fs.S3FileSystem(
        endpoint_override=endpoint.netloc, scheme=endpoint.scheme, region="us-east-1"
    )


def synthesize(batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(int(batch["id"][0]))
    x = rng.normal(size=(len(batch["id"]), FEATURES)).astype(np.float32)
    columns = {f"x{i}": x[:, i] for i in range(FEATURES)}
    columns["y"] = x.sum(axis=1)
    return columns


if __name__ == "__main__":
    ray.data.range(ROWS, override_num_blocks=32).map_batches(synthesize).write_parquet(
        INPUTS, filesystem=s3()
    )
    print(f"wrote {ROWS} rows to {INPUTS}")
