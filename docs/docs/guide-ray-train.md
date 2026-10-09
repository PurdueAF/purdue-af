# Training on GPUs with Ray

[Ray](https://docs.ray.io/en/latest/) trains your models, and runs them, on
GPUs your session does not hold. You submit a script as a job; a Ray cluster
of your own starts for it in a minute or two, runs it as you on
[shared GPUs](#gpus), and is [removed](#lifetime) once idle.

| To                                         | Each GPU runs                                 | GPUs                    | See                                                     |
| ------------------------------------------ | --------------------------------------------- | ----------------------- | ------------------------------------------------------- |
| Train one model                            | The training                                  | One                     | [Submitting a job](#submitting-a-job)                   |
| Train one model, faster                    | A copy of the model, on its share of the data | Up to those you ask for | [Data-parallel training](#data-parallel-training)       |
| Train many models, to tune or compare them | One of the trainings                          | Up to those you ask for | [Hyperparameter search](#hyperparameter-search)         |
| Train a model too large for a GPU          | A part of the model                           | Two, or a set number    | [A model larger than a GPU](#a-model-larger-than-a-gpu) |
| Run a model over a dataset                 | A copy of the model, on its batches           | Up to those you ask for | [Batch inference](#batch-inference)                     |
| Run any function many times                | One of the calls                              | Up to those you ask for | [Tasks](#tasks)                                         |

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

## Data-parallel training

[Ray Train](https://docs.ray.io/en/latest/train/getting-started-pytorch.html)
trains one model on several GPUs at once: every worker holds a copy of the
model and trains it on its share of the data, and the copies stay in step.
The training loop becomes a function, in which `prepare_model` and
`prepare_data_loader` put the model and the data on each worker's GPU:

??? example "train.py: a training on the GPUs your cluster has"

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

        # Started again after a worker is lost or returns: go on from the last checkpoint.
        first_epoch = 0
        checkpoint = ray.train.get_checkpoint()
        if checkpoint:
            with checkpoint.as_directory() as path:
                state = torch.load(f"{path}/state.pt")
            net.load_state_dict(state["model"])
            optimizer.load_state_dict(state["optimizer"])
            first_epoch = state["epoch"] + 1

        loss_fn = nn.BCEWithLogitsLoss()
        for epoch in range(first_epoch, 5):
            for X_batch, y_batch in loader:
                loss = loss_fn(model(X_batch), y_batch)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            # Every worker reports; the first one saves the training with its report.
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


    trainer = TorchTrainer(
        train_func,
        # From one worker to the most the submission asks for: those your cluster has.
        scaling_config=ScalingConfig(
            num_workers=(1, int(os.environ["AF_MAX_WORKERS"])), use_gpu=True
        ),
        run_config=RunConfig(
            storage_path="/work/users/<username>/ray_results",
            failure_config=FailureConfig(max_failures=10),
        ),
    )
    result = trainer.fit()
    print(result.checkpoint.path)
    ```

Submit it with the most GPUs it may have, and without `entrypoint_num_gpus`,
since the trainer's workers hold the GPUs:

```python
client = JobSubmissionClient(
    "http://ray-train-gateway:8265", headers={"af-max-workers": "4"}
)
job = client.submit_job(entrypoint="python train.py", runtime_env={"working_dir": "."})
```

* The script reads that number from `AF_MAX_WORKERS`, and names none itself.
  `num_workers=(1, N)` is
  [elastic training](https://docs.ray.io/en/latest/train/user-guides/elastic-training.html):
  on the workers your cluster has, from one to N.
* When your cluster [loses a worker or gets one back](#gpus), the function
  starts again on the workers there are, from the
  [checkpoint](https://docs.ray.io/en/latest/train/user-guides/checkpoints.html)
  it last reported. `max_failures` is how many such losses a training goes
  through.
* `storage_path` must be on `/work`: with the default, a training stops at its
  first checkpoint with *Unable to set up cluster storage*.
* The script prints the last checkpoint's directory, which your session reads
  like any other: `torch.load("<path>/state.pt", map_location="cpu")["model"]`.
* Every worker adds a batch of its own to each step, so a training differs
  with the number of workers. For one that keeps its number, submit with
  [`af-n-workers`](#cluster-settings) in place of `af-max-workers`, and train
  with `num_workers=int(os.environ["AF_MAX_WORKERS"])`.

### Data in files

[Ray Data](https://docs.ray.io/en/latest/train/user-guides/data-loading-preprocessing.html)
reads and prepares files on the workers' CPU cores as the training goes, and
deals the rows out among the workers, so the data need not fit in memory. It
takes the place of the `DataLoader` and of `prepare_data_loader`:

```python
def train_func():
    ...
    data = ray.train.get_dataset_shard("train")
    for epoch in range(first_epoch, 5):
        # A batch is a dict of tensors by column, on this worker's GPU.
        for batch in data.iter_torch_batches(batch_size=512):
            ...


trainer = TorchTrainer(
    train_func,
    datasets={"train": ray.data.read_parquet("/work/users/<username>/events")},
    ...
)
```

### Other libraries

* XGBoost trains on the workers' GPUs with
  [`XGBoostTrainer`](https://docs.ray.io/en/latest/train/getting-started-xgboost.html)
  and `"device": "cuda"`, from the global Pixi environment as PyTorch does.
* [Lightning](https://docs.ray.io/en/latest/train/getting-started-pytorch-lightning.html)
  and [Transformers](https://docs.ray.io/en/latest/train/getting-started-transformers.html)
  training loops run in the same `TorchTrainer`, from an
  [environment of your own](#environment) that has them.

## Hyperparameter search

[Ray Tune](https://docs.ray.io/en/latest/tune/index.html) runs one training
per set of hyperparameters, each on a GPU of its own and as many at once as
your cluster has workers, picks the values itself and stops the trainings that
fall behind. The function reports its loss as it trains:

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
        run_config=tune.RunConfig(
            storage_path="/work/users/<username>/ray_results",
            failure_config=tune.FailureConfig(max_failures=10),
        ),
    )
    best = tuner.fit().get_best_result()
    print(best.config, best.metrics["loss"])
    ```

Submit it as for [data-parallel training](#data-parallel-training):
`af-max-workers` is the most trainings to run at once, and the script names no
number of GPUs.

* A training whose worker [loses its GPU](#gpus) starts again on the next free
  one, up to `max_failures` times: from its beginning, or from its last
  [checkpoint](https://docs.ray.io/en/latest/tune/tutorials/tune-trial-checkpoints.html)
  if it saves them.
* `storage_path` must be on `/work`, as for Ray Train.
* [`tune.grid_search`](https://docs.ray.io/en/latest/tune/api/doc/ray.tune.grid_search.html)
  in place of `tune.loguniform` runs one training per value it lists: the
  folds of a cross-validation, or the members of an ensemble.
* A [search algorithm](https://docs.ray.io/en/latest/tune/api/suggestion.html)
  picks each set of values from the results so far: `search_alg=OptunaSearch()`
  works from the global Pixi environment. A
  [scheduler](https://docs.ray.io/en/latest/tune/api/schedulers.html) other
  than `ASHAScheduler`, such as population-based training, decides otherwise
  which trainings go on.
* For trainings that each run on several GPUs, a search
  [starts a Ray Train run per training](https://docs.ray.io/en/latest/train/user-guides/hyperparameter-optimization.html).

## A model larger than a GPU

A model that needs more memory than one GPU has runs on the two GPUs of one
worker, or on the GPUs of several workers.

### Two GPUs in one worker

The script puts each part of the model on one of a worker's two T4s, or leaves
that to
[`device_map="auto"`](https://huggingface.co/docs/accelerate/usage_guides/big_modeling)
for a Hugging Face model:

```python
first = nn.Sequential(nn.Linear(20, 4096), nn.ReLU()).to("cuda:0")
second = nn.Linear(4096, 1).to("cuda:1")


def model(x):
    return second(first(x.to("cuda:0")).to("cuda:1"))
```

Submit it with two GPUs for the worker, and both for the script:

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

* The script is plain PyTorch: Ray only
  [gives it the GPUs](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/sdk.html#specifying-cpu-and-gpu-resources).
* It has exactly two GPUs from start to end: your cluster starts only while
  two T4s are free, and [keeps](#gpus) its first worker.
* A part of a model cannot be on another worker's GPU this way.

### Several workers

[FSDP](https://docs.pytorch.org/docs/stable/fsdp.html) is
[data-parallel training](#data-parallel-training) in which every worker keeps
a share of the parameters, of their gradients and of the optimizer's state,
and gets a layer's parameters from the others when it computes that layer. In
`train.py`:

```python
from torch.distributed.fsdp.wrap import ModuleWrapPolicy

model = ray.train.torch.prepare_model(
    net,
    parallel_strategy="fsdp",
    # The layers a GPU holds whole, one at a time.
    parallel_strategy_kwargs={"auto_wrap_policy": ModuleWrapPolicy({nn.Linear})},
)
```

* It saves the memory of the parameters only: a GPU still holds the
  activations of its batches, and one whole layer at a time.
* The fewer the workers, the more each one holds: submit with
  [`af-n-workers`](#cluster-settings), and train with
  `num_workers=int(os.environ["AF_MAX_WORKERS"])`.
* Every worker takes part in a checkpoint, with FSDP's own calls:
  `FSDP.state_dict_type` with `rank0_only` to save the model, and
  `FSDP.optim_state_dict` and `FSDP.optim_state_dict_to_load` for the
  optimizer.
* Every step moves parameters between the workers, which makes it slower than
  training a model that fits.

## Batch inference

[Ray Data](https://docs.ray.io/en/latest/data/batch_inference.html) runs a
model over a dataset: every worker holds a copy of the model and takes batches
as they are read, and the results are written as they come.

??? example "score.py: the scores of a trained model for every row of a dataset"

    ```python
    # score.py
    import os

    import numpy as np
    import ray
    import torch
    from torch import nn

    FEATURES = [f"f{i}" for i in range(20)]


    class Score:
        def __init__(self):
            state = torch.load("<checkpoint>/state.pt")
            self.model = nn.Sequential(nn.Linear(20, 64), nn.ReLU(), nn.Linear(64, 1))
            self.model.load_state_dict(state["model"])
            self.model.cuda()

        def __call__(self, batch):
            x = torch.as_tensor(np.stack([batch[name] for name in FEATURES], axis=1))
            with torch.inference_mode():
                batch["score"] = self.model(x.cuda()).squeeze(1).cpu().numpy()
            return batch


    events = ray.data.read_parquet("/work/users/<username>/events")
    events.map_batches(
        Score,
        num_gpus=1,
        batch_size=4096,
        # From one copy of the model to the most the submission asks for.
        compute=ray.data.ActorPoolStrategy(
            min_size=1, max_size=int(os.environ["AF_MAX_WORKERS"])
        ),
    ).write_parquet("/work/users/<username>/scores")
    ```

Submit it as for [data-parallel training](#data-parallel-training):
`af-max-workers` is the most copies of the model.

* There are as many copies as your cluster has workers, and a batch whose
  worker [loses its GPU](#gpus) is run again on another.
* What a batch holds is the dataset's: here, an array per column of a Parquet
  file.

## Tasks

Any Python function runs on a GPU of your cluster as a
[Ray task](https://docs.ray.io/en/latest/ray-core/tasks.html), as many of them
at once as your cluster has workers:

```python
import ray


@ray.remote(num_gpus=1)
def evaluate(path): ...


results = ray.get([evaluate.remote(path) for path in paths])
```

Submit it as for [data-parallel training](#data-parallel-training). A task
whose worker [loses its GPU](#gpus) runs again,
[three times at most](https://docs.ray.io/en/latest/ray-core/fault_tolerance/tasks.html)
unless `max_retries` says otherwise.

## Cluster settings

Your cluster has one worker with one GPU, 8 CPU cores and 32 GB of memory, and
runs the global Pixi environment, unless the `headers` of the
`JobSubmissionClient` say otherwise. Every value is a string:

| Header                 | Sets                                                  | Values                       |
| ---------------------- | ----------------------------------------------------- | ---------------------------- |
| `af-max-workers`       | Most workers, [as GPUs are free](#gpus)               | `"1"` and up                 |
| `af-n-workers`         | Exact number of workers, in place of `af-max-workers` | `"1"` and up                 |
| `af-gpus-per-worker`   | GPUs of each worker                                   | `"1"`, or `"2"` for T4s only |
| `af-min-gpu-memory-gb` | Least memory of each GPU, in GB                       | Above `"5"` for T4s only     |
| `af-cpus-per-worker`   | CPU cores of each worker                              | `"1"` to `"16"`              |
| `af-ram-per-worker-gb` | Memory of each worker, in GB                          | `"4"` to `"64"`              |
| `af-env`               | [Environment](#environment) of the cluster            | A path                       |

A GPU is a 5 GB slice of an A100 or a 16 GB T4, whichever is free. Up to two
workers, each with a T4:

```python
client = JobSubmissionClient(
    "http://ray-train-gateway:8265",
    headers={"af-max-workers": "2", "af-min-gpu-memory-gb": "16"},
)
```

* A misspelled `af-` header fails the call, with the list of those above.
* A script gets no more GPUs than one worker has: `entrypoint_num_gpus=2`
  needs `af-gpus-per-worker` of `"2"`.
* Submitting with other settings [replaces](#lifetime) your cluster.

## Your cluster

### GPUs

The GPUs are shared with everyone's sessions and clusters:

* With `af-max-workers`, your cluster starts with as many of its workers as
  have free GPUs, at least one, and gets no more of them later. With
  `af-n-workers`, it starts only while the GPUs of all its workers are free.
  A submission whose cluster cannot start fails with the number of workers
  Ray clusters can take.
* Your cluster keeps its first worker. The others may lose their GPUs at any
  time to a session or to another cluster's first worker, and return when
  GPUs are free again. [Data-parallel training](#data-parallel-training), a
  [hyperparameter search](#hyperparameter-search),
  [batch inference](#batch-inference) and [tasks](#tasks) go on with the
  workers left.

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

The environment needs PyTorch and Ray 2.52 or later, 2.55 for
`num_workers=(1, N)` (`pixi add --pypi "ray[default,train]" torch`), on
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
