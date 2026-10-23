"""Writes the training inputs to NRP S3 with Ray Data, from an AF session: a
synthetic regression set in the columns train.py reads."""

import numpy as np
import ray
from train import FEATURES, INPUTS, s3

ROWS = 4_000_000


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
