# Training on GPUs with Ray Train

[Ray Train](https://docs.ray.io/en/latest/train/train.html) runs a PyTorch
training loop on a GPU that is yours only while your jobs need it. Your
session does not need a GPU of its own: you submit the training script from a
terminal and follow its output there.

Your jobs run in a Ray cluster of your own, with one **NVIDIA T4** GPU (16 GB).
It starts when you first submit, runs as you, sees your storage at the same
paths as your session, and is removed after some minutes without jobs. No
other user can see, stop or reach it.

## 1. Adapt the training script

Four changes turn a PyTorch training script into a Ray Train job:

1. Move the training loop into a function that takes a `config` dictionary.
2. Pass the model through `ray.train.torch.prepare_model()` and the
   `DataLoader` through `ray.train.torch.prepare_data_loader()`, and drop your
   own `.to("cuda")` calls: these put the model and every batch on the GPU.
3. Call `ray.train.report()` once per epoch, with the metrics and, optionally,
   a checkpoint.
4. Run the function with a `TorchTrainer` that trains on one GPU and stores
   its results in a directory of yours.

A complete `train.py`, with random numbers standing in for your data:

```python
import os
import tempfile

import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

import ray.train
import ray.train.torch
from ray.train import Checkpoint, RunConfig, ScalingConfig
from ray.train.torch import TorchTrainer


def train_func(config):
    # Everything in this function runs on the GPU.
    X = torch.randn(100_000, 20)
    y = (X.sum(dim=1, keepdim=True) > 0).float()
    loader = DataLoader(
        TensorDataset(X, y), batch_size=config["batch_size"], shuffle=True
    )
    loader = ray.train.torch.prepare_data_loader(loader)

    model = nn.Sequential(nn.Linear(20, 64), nn.ReLU(), nn.Linear(64, 1))
    model = ray.train.torch.prepare_model(model)
    optimizer = torch.optim.Adam(model.parameters(), lr=config["lr"])
    loss_fn = nn.BCEWithLogitsLoss()

    for epoch in range(config["epochs"]):
        for X_batch, y_batch in loader:  # batches arrive on the GPU
            loss = loss_fn(model(X_batch), y_batch)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        with tempfile.TemporaryDirectory() as tmp:
            torch.save(model.state_dict(), os.path.join(tmp, "model.pt"))
            ray.train.report(
                {"epoch": epoch, "loss": loss.item()},
                checkpoint=Checkpoint.from_directory(tmp),
            )


trainer = TorchTrainer(
    train_func,
    train_loop_config={"lr": 1e-3, "batch_size": 512, "epochs": 5},
    scaling_config=ScalingConfig(num_workers=1, use_gpu=True),
    run_config=RunConfig(
        storage_path="/work/users/<username>/ray-results", name="example"
    ),
)
result = trainer.fit()
print(result.metrics)
print(result.checkpoint.path)
```

## 2. Submit the job

Your session already knows where your Ray cluster is and how to sign in to it.
In a terminal, set up the Ray command line once (`uvx` runs Ray from an
environment of its own, so none of your environments change):

```shell
alias ray='uvx --from "ray[default]" ray'
```

Then, in the directory that holds `train.py`:

```shell
ray job submit --working-dir . --runtime-env-json '{"pip": ["torch"]}' -- python train.py
```

* `--working-dir .` uploads the directory to your cluster. Keep it to code: the
  upload has a size limit, and the job reads its data from storage (see
  [Data and results](#data-and-results)).
* `"pip"` lists the packages your code imports; add the ones beyond `torch`,
  with versions where they matter. Your cluster installs them the first time
  a job asks for them, which takes a minute or two.
* The first submission starts your cluster, which takes a minute or two when
  a T4 is free. When none is, the submission waits for one and gives up with a
  message after a while.
* The command prints the job's output until the job ends. `Ctrl+C` stops the
  printing, not the job.

In a notebook, run the same command in a `!` cell, with the alias written out:
`!uvx --from "ray[default]" ray job submit ...`.

## 3. Follow and stop jobs

```shell
ray job list                     # your jobs
ray job logs --follow <job-id>   # the output of one job
ray job stop <job-id>
```

`<job-id>` is the `raysubmit_...` identifier that `ray job submit` prints.

## Data and results

Your cluster sees `/work`, `/depot/cms`, `/eos` and `/cvmfs` at the same paths
as your session, with your permissions, so your data paths work unchanged.
Your home directory is not there: code travels with `--working-dir`.

Checkpoints go to `<storage_path>/<name>/`, and the example prints the path of
the last one. Load it from your session like any file:

```python
import torch

weights = torch.load("/work/users/<username>/ray-results/example/<checkpoint>/model.pt")
```

## Good to know

* Your cluster has one GPU, so `num_workers` stays at 1: a job that asks for
  more waits indefinitely.
* A cluster left without jobs for some minutes is removed, and its job history
  with it: `ray job list` then shows nothing, while everything your jobs wrote
  stays where they wrote it.
