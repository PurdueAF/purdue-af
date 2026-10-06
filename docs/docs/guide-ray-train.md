# Training on GPUs with Ray

[Ray](https://docs.ray.io/en/latest/) trains your PyTorch models on GPUs your
session does not hold. You submit a script as a job; a Ray cluster of your own
starts for it in a minute or two, runs it as you on [shared GPUs](#gpus), and
is [removed](#lifetime) once idle.

## Before you start

Available to all users. You need:

* **A session started without a GPU.** From a session
  [that holds one](gpus.md#1-direct-connection), submissions fail.
* **The Python (pixi global) kernel**, or
  [an environment of your own](#environment) with Ray and PyTorch.
* **Data outside your home directory**, which your cluster
  [does not see](#data-and-results).

## Submitting a job

A [Ray job](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/sdk.html)
is a script your cluster runs by itself, whether or not the notebook that
submitted it stays open. With `train.py` beside a notebook on the
**Python (pixi global)** kernel:

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
* `working_dir` sends the notebook's directory along, the script and its
  modules with it. Keep data out of that directory.

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

## From a PyTorch script

A script that already trains on one GPU runs as it is, once it:

* **reads and writes outside your home directory**, the model and checkpoints
  it saves included;
* **takes its settings from the command line or a file**, not from notebook
  variables: `entrypoint="python train.py --epochs 20"`;
* **prints what you want to follow**: its output is the job's log.

For one model on several GPUs, move the training loop into a function for
[Ray Train](#ray-train); to compare many models, see [Ray Tune](#ray-tune).

## Cluster settings

Your cluster has one worker with one GPU, 8 CPU cores and 32 GB of memory, and
runs the global Pixi environment, unless the `headers` of the
`JobSubmissionClient` say otherwise. Every value is a string:

| Header                 | Sets                                       | Values                              |
| ---------------------- | ------------------------------------------ | ----------------------------------- |
| `af-n-workers`         | Number of workers                          | `"1"` and up                        |
| `af-gpus-per-worker`   | GPUs of each worker                        | `"1"`, or `"2"` for T4s only        |
| `af-min-gpu-memory-gb` | Least memory of each GPU, in GB            | Above `"5"` for T4s only            |
| `af-cpus-per-worker`   | CPU cores of each worker                   | `"1"` to `"16"`                     |
| `af-ram-per-worker-gb` | Memory of each worker, in GB               | `"4"` to `"64"`                     |
| `af-env`               | [Environment](#environment) of the cluster | A path                              |

A GPU is a 5 GB slice of an A100 or a 16 GB T4, whichever is free. Two
workers, each with a T4:

```python
client = JobSubmissionClient(
    "http://ray-train-gateway:8265",
    headers={"af-n-workers": "2", "af-min-gpu-memory-gb": "16"},
)
```

* A misspelled `af-` header fails the call, with the list of those above.
* A script gets no more GPUs than one worker has: `entrypoint_num_gpus=2`
  needs `af-gpus-per-worker` of `"2"`.
* Submitting with other settings [replaces](#lifetime) your cluster.

## Ray Train

[Ray Train](https://docs.ray.io/en/latest/train/getting-started-pytorch.html)
runs one training on all the GPUs of your cluster and keeps its checkpoints.
The training loop becomes a function, in which `prepare_model` and
`prepare_data_loader` put the model and the data on each worker's GPU:

??? example "train.py: a training on two GPUs"

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
GPUs, and ask for as many workers as it has:

```python
client = JobSubmissionClient(
    "http://ray-train-gateway:8265", headers={"af-n-workers": "2"}
)
job = client.submit_job(entrypoint="python train.py", runtime_env={"working_dir": "."})
```

* `storage_path` must be on `/work`: with the default, a training stops at its
  first checkpoint with *Unable to set up cluster storage*.
* The script prints the last checkpoint's directory, which your session reads
  like any other: `torch.load("<path>/model.pt", map_location="cpu")`.

## Ray Tune

[Ray Tune](https://docs.ray.io/en/latest/tune/index.html) runs one training
per set of hyperparameters, each on a GPU of its own, picks the values itself
and stops the trainings that fall behind. The function reports its loss as it
trains:

??? example "search.py: a search over the learning rate"

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

Submit it as for [Ray Train](#ray-train), with `af-n-workers` the number of
trainings to run at once and the same rule for `storage_path`.

## Your cluster

### GPUs

The GPUs are shared with everyone's sessions and clusters:

* Your cluster starts only while all the GPUs it asks for are free. Otherwise
  the submission fails with the number of workers it could have: ask for
  fewer, or try again later.
* Your cluster keeps its first worker. The others may lose their GPUs at any
  time to a session or to another cluster's first worker, and return when
  GPUs are free again. A training that
  [saves checkpoints](https://docs.ray.io/en/latest/train/user-guides/fault-tolerance.html)
  and sets `FailureConfig(max_failures=...)` resumes from the last one.

### Environment

Your cluster runs the
[global Pixi environment](software.md#the-global-pixi-environment) and needs
the same Python and Ray as the notebook that submits to it. From a notebook on
the **Python (pixi project-aware)** kernel, name that kernel's environment:

```python
import os
import sys

client = JobSubmissionClient(
    "http://ray-train-gateway:8265",
    headers={"af-env": os.path.realpath(sys.prefix)},
)
```

The environment needs PyTorch and Ray 2.52 or later
(`pixi add --pypi "ray[default,train]" torch`), on
[storage your cluster sees](#data-and-results).

### Data and results

Your cluster sees `/work`, `/depot/cms`, `/eos/purdue` and `/cvmfs` at the same
paths as your session, with your permissions, and not your home directory
(see [Storage volumes](storage.md#overview)). Have a job write what you keep,
the model included, to `/work`.

### Lifetime

* Your cluster runs one set of [settings](#cluster-settings) at a time.
  Submitting with other settings, the defaults included, replaces it when
  nothing runs on it; while something does, the submission fails.
* A cluster with nothing running for some minutes is removed, with the
  records and logs of its jobs.
