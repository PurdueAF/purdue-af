# Training on GPUs with Ray

[Ray](https://docs.ray.io/en/latest/) is a Python library that runs your
PyTorch script on GPUs outside your session, on one of them or on several at
once. You submit the script from a notebook and read its output there.

| To                                  | Each GPU runs                               | See                                                     |
| ----------------------------------- | ------------------------------------------- | ------------------------------------------------------- |
| Train a model                       | The training                                | [One GPU](#one-gpu)                                     |
| Train a model faster                | A copy of the model, on a share of the data | [Data-parallel training](#data-parallel-training)       |
| Try many hyperparameters            | A training with one set of them             | [Hyperparameter search](#hyperparameter-search)         |
| Train a model too large for one GPU | A part of the model                         | [A model larger than a GPU](#a-model-larger-than-a-gpu) |
| Apply a trained model to a dataset  | A copy of the model, on a share of the rows | [Batch inference](#batch-inference)                     |

## Before you start

Available to all users.

* Start your session **without a GPU**.
* Use the **Python (pixi global)** kernel, which has every package this guide
  uses, or [an environment of your own](#your-own-environment).
* Keep your data and results on `/work` or `/depot`: a job does not see your
  home directory.

## One GPU

A PyTorch script that trains on `cuda` runs as it is:

```python
# train.py
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

X = torch.randn(100_000, 20)
y = (X.sum(dim=1, keepdim=True) > 0).float()
loader = DataLoader(TensorDataset(X, y), batch_size=512, shuffle=True)

model = nn.Sequential(nn.Linear(20, 64), nn.ReLU(), nn.Linear(64, 1)).to("cuda")
optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
loss_fn = nn.BCEWithLogitsLoss()

for epoch in range(5):
    for X_batch, y_batch in loader:
        loss = loss_fn(model(X_batch.to("cuda")), y_batch.to("cuda"))
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    print(f"epoch {epoch}: loss {loss.item():.3f}")

torch.save(model.state_dict(), "/work/users/<username>/model.pt")
```

Submit it from a notebook in the same directory:

```python
from ray.job_submission import JobSubmissionClient

client = JobSubmissionClient("http://ray-train-gateway:8265")
job = client.submit_job(
    entrypoint="python train.py",
    # Sends the notebook's directory, the script with it.
    runtime_env={"working_dir": "."},
    entrypoint_num_gpus=1,
)
```

The job starts within a minute or two, and goes on if you close the notebook.
What the script prints is the job's log:

```python
client.get_job_status(job)
print(client.get_job_logs(job))
client.stop_job(job)
```

## Data-parallel training

Every GPU holds a copy of the model and trains it on its own share of each
epoch, and the copies stay in step, as with PyTorch's
`DistributedDataParallel`.
[Ray Train](https://docs.ray.io/en/latest/train/getting-started-pytorch.html)
sets this up. The numbered comments mark what changes in the script of
[One GPU](#one-gpu):

```python
# train.py
import os
import tempfile

import ray.train
import ray.train.torch
import torch
from ray.train import Checkpoint, FailureConfig, RunConfig, ScalingConfig
from ray.train.torch import TorchTrainer
from torch import nn
from torch.utils.data import DataLoader, TensorDataset


# 1. The training goes into a function.
def train_func():
    X = torch.randn(100_000, 20)
    y = (X.sum(dim=1, keepdim=True) > 0).float()
    loader = DataLoader(TensorDataset(X, y), batch_size=512, shuffle=True)
    net = nn.Sequential(nn.Linear(20, 64), nn.ReLU(), nn.Linear(64, 1))

    # 2. Ray Train puts the model and the batches on the GPU, in place of
    #    .to("cuda"), and gives each GPU its share of the data.
    model = ray.train.torch.prepare_model(net)
    loader = ray.train.torch.prepare_data_loader(loader)

    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.BCEWithLogitsLoss()

    # 3. The training starts from its last checkpoint, if it has one.
    first_epoch = 0
    checkpoint = ray.train.get_checkpoint()
    if checkpoint:
        with checkpoint.as_directory() as path:
            state = torch.load(f"{path}/state.pt")
        net.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        first_epoch = state["epoch"] + 1

    for epoch in range(first_epoch, 5):
        for X_batch, y_batch in loader:
            loss = loss_fn(model(X_batch), y_batch)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        # 4. Every GPU reports the epoch, in place of print, and the first
        #    one saves a checkpoint with it.
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = None
            if ray.train.get_context().get_world_rank() == 0:
                state = {
                    "model": net.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "epoch": epoch,
                }
                torch.save(state, f"{tmp}/state.pt")
                checkpoint = Checkpoint.from_directory(tmp)
            ray.train.report({"loss": loss.item()}, checkpoint=checkpoint)


# 5. Ray Train runs the function on the GPUs.
trainer = TorchTrainer(
    train_func,
    scaling_config=ScalingConfig(
        # From one GPU to as many as you ask for when you submit.
        num_workers=(1, int(os.environ["AF_MAX_WORKERS"])),
        use_gpu=True,
    ),
    run_config=RunConfig(
        # Where the checkpoints go: a directory of yours on /work.
        storage_path="/work/users/<username>/ray_results",
        # Lets the training go on when it loses a GPU.
        failure_config=FailureConfig(max_failures=10),
    ),
)
result = trainer.fit()
print(result.checkpoint.path)
```

Submit it with the number of GPUs to train on, and without
`entrypoint_num_gpus`:

```python
client = JobSubmissionClient(
    "http://ray-train-gateway:8265", headers={"af-max-workers": "4"}
)
job = client.submit_job(entrypoint="python train.py", runtime_env={"working_dir": "."})
```

The training takes as many of the four GPUs as are free. Its last checkpoint
is the directory the script prints, with the model in `state.pt`.

For data in files that do not fit in memory,
[Ray Data](https://docs.ray.io/en/latest/train/user-guides/data-loading-preprocessing.html)
takes the place of the `DataLoader` and of `prepare_data_loader`:

```python
def train_func():
    ...
    data = ray.train.get_dataset_shard("train")
    for epoch in range(first_epoch, 5):
        # A batch is a dict of tensors by column, on the GPU.
        for batch in data.iter_torch_batches(batch_size=512):
            ...


trainer = TorchTrainer(
    train_func,
    datasets={"train": ray.data.read_parquet("/work/users/<username>/events")},
    ...
)
```

Ray Train has guides of its own for
[Lightning](https://docs.ray.io/en/latest/train/getting-started-pytorch-lightning.html),
[Transformers](https://docs.ray.io/en/latest/train/getting-started-transformers.html)
and [XGBoost](https://docs.ray.io/en/latest/train/getting-started-xgboost.html).

## Hyperparameter search

Every GPU trains the model with a different set of hyperparameters.
[Ray Tune](https://docs.ray.io/en/latest/tune/index.html) chooses the sets,
stops the trainings that fall behind and returns the best one. The numbered
comments mark what changes in the script of [One GPU](#one-gpu):

```python
# search.py
import torch
from ray import tune
from ray.tune.schedulers import ASHAScheduler
from torch import nn
from torch.utils.data import DataLoader, TensorDataset


# 1. The training goes into a function of its hyperparameters.
def train(config):
    X = torch.randn(100_000, 20)
    y = (X.sum(dim=1, keepdim=True) > 0).float()
    loader = DataLoader(TensorDataset(X, y), batch_size=512, shuffle=True)

    model = nn.Sequential(nn.Linear(20, 64), nn.ReLU(), nn.Linear(64, 1)).to("cuda")
    optimizer = torch.optim.Adam(model.parameters(), lr=config["lr"])
    loss_fn = nn.BCEWithLogitsLoss()

    for epoch in range(5):
        for X_batch, y_batch in loader:
            loss = loss_fn(model(X_batch.to("cuda")), y_batch.to("cuda"))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        # 2. It reports each epoch, in place of print.
        tune.report({"loss": loss.item()})


# 3. Ray Tune runs the function once per set of hyperparameters, each on a GPU.
tuner = tune.Tuner(
    tune.with_resources(train, {"gpu": 1}),
    param_space={"lr": tune.loguniform(1e-4, 1e-1)},
    tune_config=tune.TuneConfig(
        metric="loss",
        mode="min",
        num_samples=20,
        # Stops the trainings that fall behind.
        scheduler=ASHAScheduler(),
    ),
    run_config=tune.RunConfig(
        storage_path="/work/users/<username>/ray_results",
        # Lets a training start again when it loses its GPU.
        failure_config=tune.FailureConfig(max_failures=10),
    ),
)
best = tuner.fit().get_best_result()
print(best.config, best.metrics["loss"])
```

Submit it as for [data-parallel training](#data-parallel-training):
`af-max-workers` is the number of trainings to run at once.

Ray Tune can also
[run a list of values](https://docs.ray.io/en/latest/tune/api/doc/ray.tune.grid_search.html),
[choose them with Optuna](https://docs.ray.io/en/latest/tune/api/suggestion.html)
and [search over data-parallel trainings](https://docs.ray.io/en/latest/train/user-guides/hyperparameter-optimization.html).

## A model larger than a GPU

### On two GPUs

The script puts each part of the model on a GPU. The numbered comments mark
what changes in the script of [One GPU](#one-gpu):

```python
# train.py
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

X = torch.randn(100_000, 20)
y = (X.sum(dim=1, keepdim=True) > 0).float()
loader = DataLoader(TensorDataset(X, y), batch_size=512, shuffle=True)

# 1. Each part of the model goes on a GPU of its own.
first = nn.Sequential(nn.Linear(20, 4096), nn.ReLU()).to("cuda:0")
second = nn.Linear(4096, 1).to("cuda:1")
optimizer = torch.optim.Adam([*first.parameters(), *second.parameters()], lr=1e-3)
loss_fn = nn.BCEWithLogitsLoss()

for epoch in range(5):
    for X_batch, y_batch in loader:
        # 2. The data follows the model from one GPU to the other.
        hidden = first(X_batch.to("cuda:0"))
        loss = loss_fn(second(hidden.to("cuda:1")), y_batch.to("cuda:1"))
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    print(f"epoch {epoch}: loss {loss.item():.3f}")
```

Submit it with two GPUs:

```python
client = JobSubmissionClient(
    "http://ray-train-gateway:8265", headers={"af-gpus-per-worker": "2"}
)
job = client.submit_job(
    entrypoint="python train.py",
    runtime_env={"working_dir": "."},
    entrypoint_num_gpus=2,
)
```

The two GPUs are T4s, with 16 GB each.

### On more GPUs

[FSDP](https://docs.pytorch.org/docs/stable/fsdp.html) is
[data-parallel training](#data-parallel-training) in which every GPU keeps
only its share of the model's parameters, and gets the rest of a layer from
the others when it computes that layer. It is slower than training a model
that fits. The numbered comments mark what changes in the data-parallel
script:

??? example "train.py with FSDP"

    ```python
    # train.py
    import os
    import tempfile

    import ray.train
    import ray.train.torch
    import torch
    from ray.train import Checkpoint, FailureConfig, RunConfig, ScalingConfig
    from ray.train.torch import TorchTrainer
    from torch import nn
    from torch.distributed.fsdp import FullStateDictConfig, StateDictType
    from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
    from torch.distributed.fsdp.wrap import ModuleWrapPolicy
    from torch.utils.data import DataLoader, TensorDataset


    def train_func():
        X = torch.randn(100_000, 20)
        y = (X.sum(dim=1, keepdim=True) > 0).float()
        loader = DataLoader(TensorDataset(X, y), batch_size=512, shuffle=True)
        net = nn.Sequential(nn.Linear(20, 64), nn.ReLU(), nn.Linear(64, 1))

        # 1. Each GPU keeps a share of the parameters, and holds one layer whole
        #    at a time.
        model = ray.train.torch.prepare_model(
            net,
            parallel_strategy="fsdp",
            parallel_strategy_kwargs={"auto_wrap_policy": ModuleWrapPolicy({nn.Linear})},
        )
        loader = ray.train.torch.prepare_data_loader(loader)

        optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
        loss_fn = nn.BCEWithLogitsLoss()

        # 2. A checkpoint is loaded through FSDP.
        first_epoch = 0
        checkpoint = ray.train.get_checkpoint()
        if checkpoint:
            with checkpoint.as_directory() as path:
                state = torch.load(f"{path}/state.pt")
            model.load_state_dict(state["model"])
            optimizer.load_state_dict(
                FSDP.optim_state_dict_to_load(model, optimizer, state["optimizer"])
            )
            first_epoch = state["epoch"] + 1

        for epoch in range(first_epoch, 5):
            for X_batch, y_batch in loader:
                loss = loss_fn(model(X_batch), y_batch)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

            # 3. It is saved through FSDP too: the GPUs put the model together,
            #    and hand it to the first one.
            whole = FullStateDictConfig(offload_to_cpu=True, rank0_only=True)
            with FSDP.state_dict_type(model, StateDictType.FULL_STATE_DICT, whole):
                state = {
                    "model": model.state_dict(),
                    "optimizer": FSDP.optim_state_dict(model, optimizer),
                    "epoch": epoch,
                }
            with tempfile.TemporaryDirectory() as tmp:
                checkpoint = None
                if ray.train.get_context().get_world_rank() == 0:
                    torch.save(state, f"{tmp}/state.pt")
                    checkpoint = Checkpoint.from_directory(tmp)
                ray.train.report({"loss": loss.item()}, checkpoint=checkpoint)


    trainer = TorchTrainer(
        train_func,
        scaling_config=ScalingConfig(
            # 4. The number of GPUs is fixed: each holds its share of the model.
            num_workers=int(os.environ["AF_MAX_WORKERS"]),
            use_gpu=True,
        ),
        run_config=RunConfig(
            # Where the checkpoints go: a directory of yours on /work.
            storage_path="/work/users/<username>/ray_results",
            # Lets the training go on when it loses a GPU.
            failure_config=FailureConfig(max_failures=10),
        ),
    )
    result = trainer.fit()
    print(result.checkpoint.path)
    ```

Submit it with [`af-n-workers`](#settings) in place of `af-max-workers`, for
exactly that many GPUs.

## Batch inference

Every GPU holds a copy of a trained model, here the one that
[One GPU](#one-gpu) saved, and scores its share of a dataset.
[Ray Data](https://docs.ray.io/en/latest/data/batch_inference.html) reads the
files, hands the rows to the GPUs in batches and writes the results:

```python
# score.py
import os

import numpy as np
import ray
import torch
from torch import nn

FEATURES = [f"f{i}" for i in range(20)]


class Score:
    # 1. Each GPU loads the model once.
    def __init__(self):
        self.model = nn.Sequential(nn.Linear(20, 64), nn.ReLU(), nn.Linear(64, 1))
        self.model.load_state_dict(torch.load("/work/users/<username>/model.pt"))
        self.model.to("cuda")

    # 2. It gets the rows in batches, an array per column, and adds the scores.
    def __call__(self, batch):
        x = torch.as_tensor(np.stack([batch[name] for name in FEATURES], axis=1))
        with torch.inference_mode():
            batch["score"] = self.model(x.to("cuda")).squeeze(1).cpu().numpy()
        return batch


# 3. Ray Data reads the files, runs the model on the GPUs and writes the results.
events = ray.data.read_parquet("/work/users/<username>/events")
events.map_batches(
    Score,
    num_gpus=1,
    batch_size=4096,
    compute=ray.data.ActorPoolStrategy(
        # From one GPU to as many as you ask for when you submit.
        min_size=1,
        max_size=int(os.environ["AF_MAX_WORKERS"]),
    ),
).write_parquet("/work/users/<username>/scores")
```

Submit it as for [data-parallel training](#data-parallel-training):
`af-max-workers` is the number of GPUs to score on.

## Settings

The `headers` of the `JobSubmissionClient` say what your jobs run on. A
*worker* is a process with one GPU, and CPU cores and memory beside it. Every
value is a string:

| Header                 | Sets                                                            | Default              |
| ---------------------- | --------------------------------------------------------------- | -------------------- |
| `af-max-workers`       | The most GPUs to use: your jobs get as many of them as are free | `"1"`                |
| `af-n-workers`         | An exact number of GPUs: your jobs start only if all are free   |                      |
| `af-min-gpu-memory-gb` | The least memory of a GPU: `"16"` for T4s                       | Any GPU              |
| `af-gpus-per-worker`   | `"2"` for [a model on two GPUs](#on-two-gpus)                   | `"1"`                |
| `af-cpus-per-worker`   | CPU cores of each worker, up to `"16"`                          | `"8"`                |
| `af-ram-per-worker-gb` | Memory of each worker in GB, up to `"64"`                       | `"32"`               |
| `af-env`               | [An environment of your own](#your-own-environment)             | Python (pixi global) |

To submit with other settings, wait for your running jobs to end.

## Your own environment

In a [Pixi project](guide-pixi.md) of your own on `/work` or `/depot`:

```shell
pixi add python=3.12 ipykernel
pixi add --pypi "ray[default,train,tune,data]>=2.55" torch
```

| Package        | For                               |
| -------------- | --------------------------------- |
| `ray[default]` | Submitting jobs and running them  |
| `ray[train]`   | Data-parallel training            |
| `ray[tune]`    | Hyperparameter search             |
| `ray[data]`    | Data in files, batch inference    |
| `torch`        | PyTorch, with its GPU libraries   |
| `ipykernel`    | The project's notebook kernel     |

Then, from a notebook on the **Python (pixi project-aware)** kernel, submit
with the environment's path:

```python
import os
import sys

client = JobSubmissionClient(
    "http://ray-train-gateway:8265",
    headers={"af-env": os.path.realpath(sys.prefix)},
)
```

## Good to know

* **GPUs.** A GPU is a 5 GB slice of an A100 or a 16 GB T4, whichever is
  free.
* **Shared GPUs.** All your GPUs but one can be taken back while a job runs,
  and return when they are free again. The scripts of this guide carry on when
  that happens.
* **Storage.** A job sees `/work`, `/depot/cms`, `/eos/purdue` and `/cvmfs` as
  your session does, and not your home directory.
* **Logs.** A job's status and log are removed some minutes after your last
  job ends: have the script write what you keep to `/work`.
