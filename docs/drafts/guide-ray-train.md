# Training on GPUs with Ray

[Ray](https://docs.ray.io/en/latest/) runs your PyTorch training on GPUs your
session does not hold. You have a Ray cluster of your own, with one to four
[NVIDIA T4s](../docs/gpus.md): it starts when you first submit a job or
connect, which takes a minute or two, runs as you, and is
[removed](#lifetime) once idle. No other user can see, stop or reach it.

## Ways to train

From the most to the least recommended:

|     | Way                                                                           | GPUs                              | Outlasts the notebook | Use it for                                     |
| --- | ----------------------------------------------------------------------------- | --------------------------------- | --------------------- | ---------------------------------------------- |
| 1   | [Ray Train, as a job](#1-ray-train-as-a-job)                                  | 1 to 4, all in one training       | Yes                   | Most trainings, and any on several GPUs        |
| 2   | [A PyTorch script, as a job](#2-a-pytorch-script-as-a-job)                    | 1                                 | Yes                   | A script that already trains on one GPU        |
| 3   | [A function called from the notebook](#3-a-function-called-from-the-notebook) | 1 per call, up to 4 calls at once | No                    | Short trainings, trying things out             |

Ray Train called over a notebook's connection is not among them: Ray advises
running it as a job. A scan or a search of hyperparameters is
[several trainings at once](#several-trainings-at-once).

Every example runs in a notebook on the **Python (pixi global)** kernel.

## 1. Ray Train, as a job

[Ray Train](https://docs.ray.io/en/latest/train/getting-started-pytorch.html)
runs one training on all the GPUs of your cluster and keeps its checkpoints.
Put the training in a script, say `train.py` beside your notebook:

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

Submit it, asking for as many GPUs as the trainer has workers:

```python
from ray.job_submission import JobSubmissionClient

client = JobSubmissionClient(
    "http://ray-train-gateway:8265", headers={"af-ray-gpus": "2"}
)
job = client.submit_job(entrypoint="python train.py", runtime_env={"working_dir": "."})
```

* `num_workers` is the number of GPUs the training uses, from one to four.
* `storage_path` must be on `/work`. The default, `~/ray_results`, is inside
  each pod, and with it a training stops at its first checkpoint with *Unable
  to set up cluster storage*.
* The path the script prints, which the [job's log](#jobs) shows, is the last
  checkpoint's directory. Your session reads it like any other:
  `torch.load("<path>/model.pt", map_location="cpu")`.

## 2. A PyTorch script, as a job

A script that trains on one GPU runs as it is: `entrypoint_num_gpus=1` gives
it the GPU.

```python
from ray.job_submission import JobSubmissionClient

client = JobSubmissionClient("http://ray-train-gateway:8265")
job = client.submit_job(
    entrypoint="python train.py",
    runtime_env={"working_dir": "."},
    entrypoint_num_gpus=1,
)
```

Have the script write what it makes, the model included, to `/work`.

## 3. A function called from the notebook

Connect, decorate the training function with `@ray.remote(num_gpus=1)`, and
call it with `.remote()`. `ray.get` waits for the result:

```python
import ray
import torch
from torch import nn

ray.init("ray://ray-train-gateway:10001")


@ray.remote(num_gpus=1)
def train(epochs, lr):
    # Everything in this function runs on the GPU.
    X = torch.randn(100_000, 20, device="cuda")
    y = (X.sum(dim=1, keepdim=True) > 0).float()
    model = nn.Sequential(nn.Linear(20, 64), nn.ReLU(), nn.Linear(64, 1)).to("cuda")
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    for epoch in range(epochs):
        loss = nn.functional.binary_cross_entropy_with_logits(model(X), y)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    print(f"loss after {epochs} epochs: {loss.item():.4f}")
    # Your session has no GPU: send the weights back on the CPU.
    return {name: tensor.cpu() for name, tensor in model.state_dict().items()}


weights = ray.get(train.remote(epochs=100, lr=1e-2))
```

* The call lasts as long as the notebook's connection: a kernel restart, a
  closed notebook or a lost connection stops it.
* What the function prints appears in the notebook. The dashboard link
  `ray.init` shows does not open, since only the gateway reaches your cluster.
* The function is sent with the call. Modules of your own that it imports must
  be where your cluster can import them: keep them on `/work`, or connect with
  `ray.init("ray://ray-train-gateway:10001", runtime_env={"working_dir": "."})`
  to send the notebook's directory along.
* `ray.cancel(ref)` stops a call; `ray.shutdown()` disconnects the notebook.

## Several trainings at once

A cluster with several GPUs runs as many one-GPU trainings side by side, as in
a scan of hyperparameters. From the notebook, each is a call of the
[function above](#3-a-function-called-from-the-notebook):

```python
ray.init("ray://ray-train-gateway:10001", _metadata=[("af-ray-gpus", "4")])

refs = [train.remote(epochs=100, lr=lr) for lr in (1e-1, 3e-2, 1e-2, 3e-3)]
results = ray.get(refs)
```

Calls beyond the number of GPUs wait their turn, while Ray prints *No
available node types can fulfill resource requests*.

[Ray Tune](https://docs.ray.io/en/latest/tune/index.html) picks the values
itself and stops the trainings that fall behind. The function reports its loss
as it trains, and the script, say `search.py`, is a job:

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

Submit it as in [Ray Train, as a job](#1-ray-train-as-a-job), with
`af-ray-gpus` the number of trainings to run at once and the same rule for
`storage_path`. Without `entrypoint_num_gpus`, the script itself holds no GPU,
and a script that makes the calls above runs the same way.

## Jobs

A [Ray job](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/sdk.html)
is a script your cluster runs by itself. Any notebook of yours can follow and
stop it:

```python
client.get_job_status(job)
client.get_job_logs(job)
client.stop_job(job)
client.list_jobs()

# A job's output as it comes
async for lines in client.tail_job_logs(job):
    print(lines, end="")
```

* `runtime_env={"working_dir": "."}` takes the notebook's directory along, the
  script and its modules with it. Leave data out of it: your cluster reads
  `/work` itself.
* The records and logs of a job go with the [cluster](#lifetime): have the job
  write what you keep to `/work`.

## Your cluster

You have one cluster, which every way above uses. What it runs is set where
you reach it: in the `headers` of a `JobSubmissionClient`, as in
[Ray Train, as a job](#1-ray-train-as-a-job), or in the `_metadata` of
`ray.init`, as in [Several trainings at once](#several-trainings-at-once).

| Setting     | Name          | Value                        | Default                     |
| ----------- | ------------- | ---------------------------- | --------------------------- |
| GPUs        | `af-ray-gpus` | `"1"` to `"4"`               | `"1"`                       |
| Environment | `af-ray-env`  | The path of your environment | The global Pixi environment |

### GPUs

Each GPU is a T4 in a worker pod of its own, so a call that asks for more
than one never starts. `ray.cluster_resources()`, in a connected notebook,
shows the GPUs that have joined. Your cluster holds its GPUs until it is
removed or replaced.

The T4s are shared with everyone's sessions and clusters:

* Your cluster starts only while as many T4s as it asks for are free, and all
  Ray clusters together hold only a part of the facility's T4s. Otherwise a
  submission fails with the number that are free for you, and `ray.init` with
  a connection timeout: ask for fewer, or try again later.
* A cluster whose GPUs have not all joined some minutes after it started is
  removed, with the job submitted to it.

### Environment

Your cluster needs the same Python and Ray as your notebook. It runs the
[global Pixi environment](../docs/software.md#the-global-pixi-environment),
the one of the **Python (pixi global)** kernel, which has PyTorch and Ray. In a
notebook on the **Python (pixi project-aware)** kernel, name that kernel's
environment and your cluster runs it instead:

```python
import os
import sys

env = os.path.realpath(sys.prefix)
client = JobSubmissionClient(
    "http://ray-train-gateway:8265", headers={"af-ray-env": env}
)
ray.init("ray://ray-train-gateway:10001", _metadata=[("af-ray-env", env)])
```

* The environment needs Ray 2.52 or later, with its dashboard and Ray Train,
  and PyTorch: `pixi add --pypi "ray[default,train]" torch`.
* It must be on [storage your cluster sees](#data-and-results).

### Data and results

Your cluster sees `/work`, `/depot/cms`, `/eos` and `/cvmfs` at the same paths
as your session, with your permissions, so your data paths work unchanged.
Your home directory is not there. Results come back as what a function returns
or as files on `/work`, which stay after your cluster is removed.

### Lifetime

* Your cluster runs one environment and one number of GPUs at a time. Submitting
  a job or connecting with another of either, the defaults included, replaces
  it when nothing runs on it. While something does, `ray.init` fails with a
  connection timeout, and a submission with the reason.
* A cluster with nothing running for some minutes is removed. A notebook still
  connected to it then fails its next call with a disconnection error: run
  `ray.shutdown()` and `ray.init(...)` again, which starts a new cluster.
