# Training on GPUs with Ray

[Ray](https://docs.ray.io/en/latest/) runs your PyTorch training on GPUs your
session does not hold. You have a Ray cluster of your own, with one or more
[GPUs](../docs/gpus.md), NVIDIA T4s and 5 GB slices of A100s: it starts when you submit a job, which takes a
minute or two, runs as you, and is [removed](#lifetime) once idle. No other
user can see, stop or reach it.

## Submitting a job

A [Ray job](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/sdk.html)
is a script your cluster runs by itself, whether or not the notebook that
submitted it stays open. A script that already trains on one GPU runs as it
is. With `train.py` beside a notebook on the **Python (pixi global)** kernel:

```python
from ray.job_submission import JobSubmissionClient

client = JobSubmissionClient("http://ray-train-gateway:8265")
job = client.submit_job(
    entrypoint="python train.py",
    runtime_env={"working_dir": "."},
    entrypoint_num_gpus=1,
)
```

* `entrypoint_num_gpus=1` gives the script its GPU.
* `working_dir` takes the notebook's directory along, the script and its
  modules with it. Leave data out of it: your cluster
  [reads `/work` itself](#data-and-results).

Any notebook of yours can follow and stop the job:

```python
client.get_job_status(job)
client.get_job_logs(job)
client.stop_job(job)
client.list_jobs()

# A job's output as it comes
async for lines in client.tail_job_logs(job):
    print(lines, end="")
```

Two libraries of Ray do more with the same job: [Ray Train](#ray-train), the
recommended way to write a training and the only one that uses several GPUs
for it, and [Ray Tune](#ray-tune), which runs many trainings to find the best
hyperparameters.

## Ray Train

[Ray Train](https://docs.ray.io/en/latest/train/getting-started-pytorch.html)
runs one training on all the GPUs of your cluster and keeps its checkpoints:

```python
# train.py
import tempfile

import ray.train
import ray.train.torch
import torch
from ray.train import Checkpoint, RunConfig, ScalingConfig
from ray.train.torch import TorchTrainer
from torch import nn
from torch.utils.data import DataLoader, TensorDataset


def train_func():
    X = torch.randn(100_000, 20)
    y = (X.sum(dim=1, keepdim=True) > 0).float()
    loader = DataLoader(TensorDataset(X, y), batch_size=512, shuffle=True)
    net = nn.Sequential(nn.Linear(20, 64), nn.ReLU(), nn.Linear(64, 1))

    # What differs from plain PyTorch: Ray Train puts the model and the batches
    # on this worker's GPU, and splits the data among the workers.
    model = ray.train.torch.prepare_model(net)
    loader = ray.train.torch.prepare_data_loader(loader)

    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.BCEWithLogitsLoss()
    for epoch in range(5):
        for X_batch, y_batch in loader:
            loss = loss_fn(model(X_batch), y_batch)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        # Every worker reports; the first one saves the weights with its report.
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = None
            if ray.train.get_context().get_world_rank() == 0:
                torch.save(net.state_dict(), f"{tmp}/model.pt")
                checkpoint = Checkpoint.from_directory(tmp)
            ray.train.report({"loss": loss.item()}, checkpoint=checkpoint)


trainer = TorchTrainer(
    train_func,
    scaling_config=ScalingConfig(num_workers=2, use_gpu=True),
    run_config=RunConfig(storage_path="/work/users/<username>/ray_results"),
)
result = trainer.fit()
print(result.checkpoint.path)
```

Submit it without `entrypoint_num_gpus`, since the trainer's workers hold the
GPUs, and ask for as many GPUs as it has workers:

```python
client = JobSubmissionClient("http://ray-train-gateway:8265", headers={"gpus": "2"})
job = client.submit_job(entrypoint="python train.py", runtime_env={"working_dir": "."})
```

* `storage_path` must be on `/work`. The default, `~/ray_results`, is inside
  each pod, and with it a training stops at its first checkpoint with *Unable
  to set up cluster storage*.
* The path the script prints, which the job's log shows, is the last
  checkpoint's directory. Your session reads it like any other:
  `torch.load("<path>/model.pt", map_location="cpu")`.

## Ray Tune

[Ray Tune](https://docs.ray.io/en/latest/tune/index.html) runs one training
per set of hyperparameters, each on a GPU of its own, picks the values itself
and stops the trainings that fall behind. The function reports its loss as it
trains:

```python
# search.py
import torch
from ray import tune
from ray.tune.schedulers import ASHAScheduler
from torch import nn


def train(config):
    X = torch.randn(100_000, 20, device="cuda")
    y = (X.sum(dim=1, keepdim=True) > 0).float()
    model = nn.Sequential(nn.Linear(20, 64), nn.ReLU(), nn.Linear(64, 1)).to("cuda")
    optimizer = torch.optim.Adam(model.parameters(), lr=config["lr"])
    for epoch in range(100):
        loss = nn.functional.binary_cross_entropy_with_logits(model(X), y)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        tune.report({"loss": loss.item()})


tuner = tune.Tuner(
    tune.with_resources(train, {"gpu": 1}),
    param_space={"lr": tune.loguniform(1e-3, 1e-1)},
    tune_config=tune.TuneConfig(
        metric="loss", mode="min", num_samples=20, scheduler=ASHAScheduler()
    ),
    run_config=tune.RunConfig(storage_path="/work/users/<username>/ray_results"),
)
best = tuner.fit().get_best_result()
print(best.config, best.metrics["loss"])
```

Submit it as for [Ray Train](#ray-train), with `gpus` the number of
trainings to run at once and the same rule for `storage_path`. A fixed list of
values, each trained once, is
`param_space={"lr": tune.grid_search([1e-1, 1e-2, 1e-3])}` with
`num_samples=1`.

## Your cluster

You have one cluster, which every job runs on. What it runs is set in the
`headers` of the `JobSubmissionClient`:

| Setting     | Header               | Value                                    | Default                     |
| ----------- | -------------------- | ---------------------------------------- | --------------------------- |
| GPUs        | `gpus`               | A number of GPUs, as a string            | `"1"`                       |
| GPU memory  | `min-memory-per-gpu` | The GB each GPU must have, as a string   | Any GPU                     |
| Environment | `env`                | The path of your environment             | The global Pixi environment |

### GPUs

Each GPU is in a worker pod of its own, so nothing that asks for more than one
at a time, such as `entrypoint_num_gpus=2`, ever starts. A GPU is a 5 GB slice
of an A100 or a 16 GB T4, and `min-memory-per-gpu` decides which your cluster
has:

| `min-memory-per-gpu`      | Your cluster's GPUs                           |
| ------------------------- | --------------------------------------------- |
| Not set, or `"5"` or less | A100 slices and T4s, whichever are free       |
| Above `"5"`, up to `"16"` | T4s only                                      |
| Above `"16"`              | None: the submission fails, as no GPU has it  |

A training that needs 12 GB on each GPU:

```python
client = JobSubmissionClient(
    "http://ray-train-gateway:8265",
    headers={"gpus": "2", "min-memory-per-gpu": "12"},
)
```

The GPUs are
shared with everyone's sessions and clusters:

* Your cluster starts only while as many GPUs as it asks for are free, up to
  all of them. Otherwise the submission fails with the number that are free
  for you: ask for fewer, or try again later.
* A cluster of one GPU also starts while none is free and another cluster
  holds more than its first: it takes one of those.
* A cluster whose GPUs have not all joined some minutes after it started is
  removed, with the job submitted to it.
* Your cluster keeps its first GPU. Each of the others may be taken at any
  time for someone's session or another cluster's first GPU,
  which stops what runs on it: the worker
  returns when a GPU is free again. A training that
  [saves checkpoints](https://docs.ray.io/en/latest/train/user-guides/fault-tolerance.html)
  and sets `FailureConfig(max_failures=...)` resumes from the last one.

### Environment

Your cluster runs the
[global Pixi environment](../docs/software.md#the-global-pixi-environment),
the one of the **Python (pixi global)** kernel, which has PyTorch and Ray. It
needs the same Python and Ray as the notebook that submits to it, so in a
notebook on the **Python (pixi project-aware)** kernel, name that kernel's
environment and your cluster runs it instead:

```python
import os
import sys

client = JobSubmissionClient(
    "http://ray-train-gateway:8265",
    headers={"env": os.path.realpath(sys.prefix)},
)
```

* The environment needs Ray 2.52 or later, with its dashboard and Ray Train,
  and PyTorch: `pixi add --pypi "ray[default,train]" torch`.
* It must be on [storage your cluster sees](#data-and-results).

### Data and results

Your cluster sees `/work`, `/depot/cms`, `/eos` and `/cvmfs` at the same paths
as your session, with your permissions, so your data paths work unchanged.
Your home directory is not there. Have a job write what you keep, the model
included, to `/work`: its records and logs go with the cluster.

### Lifetime

* Your cluster holds its GPUs until it is removed or replaced, and runs one
  environment, one number of GPUs and one kind of them at a time. Submitting a
  job with another of any, the defaults included, replaces it when nothing runs on it. While
  something does, the submission fails with the reason.
* A cluster with nothing running for some minutes is removed. Following its
  jobs then fails with *You have no Ray cluster*.
