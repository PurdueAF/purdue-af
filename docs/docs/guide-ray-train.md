# Training on GPUs with Ray Train

[Ray Train](https://docs.ray.io/en/latest/train/train.html) runs a PyTorch
training loop on GPU pods that start when a job needs them and are released
after it ends. Your session does not need a GPU of its own: you submit the
training script from a terminal and follow its output there.

Each training worker gets one **NVIDIA T4** GPU (16 GB). A job with several
workers trains on several GPUs at once, with PyTorch
[DDP](https://docs.pytorch.org/docs/stable/notes/ddp.html).

## 1. Adapt the training script

Four changes turn a PyTorch training script into a Ray Train job:

1. Move the training loop into a function that takes a `config` dictionary.
2. Pass the model through `ray.train.torch.prepare_model()` and the
   `DataLoader` through `ray.train.torch.prepare_data_loader()`, and drop your
   own `.to("cuda")` calls. These put the model and every batch on the
   worker's GPU; with several workers, they also wrap the model in DDP and
   split the data between the workers.
3. Call `ray.train.report()` once per epoch in every worker, with the metrics
   and, from the first worker, a checkpoint.
4. Run the function with a `TorchTrainer` that stores its results in
   `/work/projects/ray-train`.

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
    # Everything in this function runs on the GPU pods.
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
    context = ray.train.get_context()

    for epoch in range(config["epochs"]):
        if context.get_world_size() > 1:
            loader.sampler.set_epoch(epoch)  # a new shuffle every epoch
        for X_batch, y_batch in loader:  # batches arrive on the GPU
            loss = loss_fn(model(X_batch), y_batch)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = None
            if context.get_world_rank() == 0:
                # With several workers the model is wrapped in DDP; save the model inside.
                weights = model.module if hasattr(model, "module") else model
                torch.save(weights.state_dict(), os.path.join(tmp, "model.pt"))
                checkpoint = Checkpoint.from_directory(tmp)
            ray.train.report(
                {"epoch": epoch, "loss": loss.item()}, checkpoint=checkpoint
            )


trainer = TorchTrainer(
    train_func,
    train_loop_config={"lr": 1e-3, "batch_size": 512, "epochs": 5},
    scaling_config=ScalingConfig(num_workers=1, use_gpu=True),
    run_config=RunConfig(
        storage_path="/work/projects/ray-train", name="<username>-example"
    ),
)
result = trainer.fit()
print(result.metrics)
print(result.checkpoint.path)
```

`num_workers` is the number of GPUs the job trains on. Give every run its own
`name`, starting with your username: it becomes the run's directory under
`/work/projects/ray-train`.

## 2. Submit the job

In a terminal in your session, set up the Ray command line and point it at the
cluster. `uvx` runs Ray from an environment of its own, so none of your
environments change:

```shell
alias ray='uvx --from "ray[default]" ray'
export RAY_ADDRESS=http://ray-train-head-svc.cms.svc.cluster.local:8265
```

Then, in the directory that holds `train.py`:

```shell
ray job submit --working-dir . --runtime-env-json '{"pip": ["torch"]}' -- python train.py
```

* `--working-dir .` uploads the directory to the cluster. Keep it to code: the
  upload has a size limit, and the job reads its data from storage (see
  [Data and results](#data-and-results)).
* `"pip"` lists the packages your code imports; add the ones beyond `torch`,
  with versions where they matter. The first job on a new GPU pod spends a
  minute or two installing them, and later jobs on that pod reuse them.
* The command prints the job's output until the job ends. `Ctrl+C` stops the
  printing, not the job.

In a notebook, set the address with `%env RAY_ADDRESS=...` and run the command
in a `!` cell, with the alias written out:
`!uvx --from "ray[default]" ray job submit ...`.

## 3. Follow and stop jobs

```shell
ray job list                     # every job on the cluster
ray job logs --follow <job-id>   # the output of one job
ray job stop <job-id>
```

`<job-id>` is the `raysubmit_...` identifier that `ray job submit` prints.

## Data and results

The GPU pods see `/work`, `/depot/cms`, `/eos` and `/cvmfs` read-only, at the
same paths as your session. They can read what every AF user can read, which
leaves out your home directory and any file that only you can read.

Checkpoints are written to `/work/projects/ray-train/<name>/`, and the example
prints the path of the last one. Load it from your session like any file:

```python
import torch

weights = torch.load("/work/projects/ray-train/<name>/<checkpoint>/model.pt")
```

The files there belong to the cluster, not to you: copy what you want to keep
into your own directory.

## Good to know

* The cluster is shared by all AF users, and jobs run under one shared
  account rather than yours. Every user can list jobs, read their output, stop
  them, and read everything under `/work/projects/ray-train`: keep
  credentials out of your code and your job's arguments.
* GPU pods take their T4s from the same pool as sessions. When none is free,
  the job waits for one, and its log reports worker-group startup timeouts
  while Ray keeps retrying.
* The cluster runs a few GPU pods at most, shared by all jobs: a job that asks
  for more workers than that waits indefinitely.
* A GPU pod stays up for a few minutes after a job ends, so a job submitted
  soon after starts on it without a new installation.
