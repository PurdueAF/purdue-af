# Training on GPUs with Ray

[Ray](https://docs.ray.io/en/latest/ray-core/walkthrough.html) runs a
function of your notebook on a GPU. Your session does not need a GPU of its
own: you add a decorator to your PyTorch training function, and calling it
runs it on the GPU.

The function runs in a Ray cluster of your own, with one
[NVIDIA T4](../docs/gpus.md) or [more](#more-than-one-gpu). It starts when your
notebook first connects or submits a job, runs as you, and is
[removed](#good-to-know) some minutes after its last work. No other user can
see, stop or reach it.

## 1. Connect

In a notebook on the **Python (pixi global)** kernel:

```python
import ray

ray.init("ray://ray-train-gateway:10001")
```

The first connection starts your cluster, which takes a minute or two. Its
GPUs join as T4s come free, and a call that asks for one waits until then:
`ray.cluster_resources()` shows the GPUs that have joined. The dashboard link
`ray.init` shows does not open, since only the gateway reaches your cluster.

## 2. Send the training to the GPU

Put the training in a function, decorate it with `@ray.remote(num_gpus=1)`,
and call it with `.remote()`. `ray.get` waits for the result:

```python
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset


@ray.remote(num_gpus=1)
def train(epochs, lr):
    # Everything in this function runs on the GPU.
    X = torch.randn(100_000, 20)
    y = (X.sum(dim=1, keepdim=True) > 0).float()
    loader = DataLoader(TensorDataset(X, y), batch_size=512, shuffle=True)

    model = nn.Sequential(nn.Linear(20, 64), nn.ReLU(), nn.Linear(64, 1)).to("cuda")
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.BCEWithLogitsLoss()
    for epoch in range(epochs):
        for X_batch, y_batch in loader:
            loss = loss_fn(model(X_batch.to("cuda")), y_batch.to("cuda"))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        print(f"epoch {epoch}: loss {loss.item():.4f}")
    # Your session has no GPU: send the weights back on the CPU.
    return {name: tensor.cpu() for name, tensor in model.state_dict().items()}


weights = ray.get(train.remote(epochs=5, lr=1e-3))
```

* What the function prints appears in the notebook.
* The function is sent with the call. Modules of your own that it imports must
  be where your cluster can import them: keep them on `/work`, or connect with
  `ray.init("ray://ray-train-gateway:10001", runtime_env={"working_dir": "."})`
  to send the notebook's directory along.
* `ray.cancel(ref)` stops a call; `ray.shutdown()` disconnects the notebook.

## More than one GPU

Your cluster has one GPU unless you ask for more when you connect, up to 4,
each a T4 in a worker pod of its own:

```python
ray.init("ray://ray-train-gateway:10001", _metadata=[("af-ray-gpus", "4")])
```

* Your cluster holds that many GPUs until it is removed or replaced.
* [Ray Train](#ray-train), which spreads one training over several GPUs, runs
  as a job.
* With an [environment of your own](#your-own-environment), name both in one
  list: `_metadata=[("af-ray-env", ...), ("af-ray-gpus", "2")]`.

## Trainings that outlast your notebook

A call from your notebook lasts as long as the notebook's connection to your
cluster: a kernel restart, a closed notebook or a lost connection stops it. A
training of hours is better submitted as a
[Ray job](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/sdk.html):
a script your cluster runs by itself, which any notebook of yours can follow
and stop.

Put the training in a script, say `train.py` beside your notebook, that writes
what it makes to `/work`, and submit it:

```python
from ray.job_submission import JobSubmissionClient

client = JobSubmissionClient("http://ray-train-gateway:8265")
job = client.submit_job(
    entrypoint="python train.py",
    runtime_env={"working_dir": "."},
    entrypoint_num_gpus=1,
)
```

* `entrypoint_num_gpus=1` gives the script a GPU, so a PyTorch training script
  runs as it is. Without it, the script has no GPU and sends its training to
  the GPUs as a notebook does, with `@ray.remote(num_gpus=1)` or Ray Train.
* `runtime_env={"working_dir": "."}` takes the notebook's directory along, the
  script and its modules with it. Leave data out of it: your cluster reads
  `/work` itself.
* `client.get_job_status(job)`, `client.get_job_logs(job)` and
  `client.stop_job(job)` work from any notebook of yours, and
  `client.list_jobs()` lists your jobs. To follow a job's output as it comes:

    ```python
    async for lines in client.tail_job_logs(job):
        print(lines, end="")
    ```

* A job runs in your cluster's environment and with its GPUs, which a client
  names in headers as `ray.init` does in `_metadata`:
  `JobSubmissionClient("http://ray-train-gateway:8265", headers={"af-ray-gpus": "4"})`.

### Ray Train

[Ray Train](https://docs.ray.io/en/latest/train/getting-started-pytorch.html)
spreads one training over several GPUs. Ray advises running it as a job rather
than over a notebook's connection. The script builds the trainer:

```python
# train.py
from ray.train import RunConfig, ScalingConfig
from ray.train.torch import TorchTrainer


def train_func():
    # The training loop each GPU runs, as in Ray Train's guide.
    ...


trainer = TorchTrainer(
    train_func,
    scaling_config=ScalingConfig(num_workers=4, use_gpu=True),
    run_config=RunConfig(storage_path="/work/users/<username>/ray_results"),
)
result = trainer.fit()
```

and the submission asks for the GPUs, with no `entrypoint_num_gpus`, so the
trainer's workers hold all four:

```python
client = JobSubmissionClient(
    "http://ray-train-gateway:8265", headers={"af-ray-gpus": "4"}
)
job = client.submit_job(entrypoint="python train.py", runtime_env={"working_dir": "."})
```

`storage_path` keeps the checkpoints where
[Data and results](#data-and-results) says.

## Your own environment

Your cluster needs the same Python and Ray as your notebook. It runs the
[global Pixi environment](../docs/software.md#the-global-pixi-environment),
the one of the **Python (pixi global)** kernel, which has PyTorch and Ray. In a
notebook on the **Python (pixi project-aware)** kernel, name its environment
when you connect, and your cluster runs that one instead:

```python
import os
import sys

import ray

ray.init(
    "ray://ray-train-gateway:10001",
    _metadata=[("af-ray-env", os.path.realpath(sys.prefix))],
)
```

* The environment needs Ray 2.52 or later, with its dashboard, and PyTorch:
  `pixi add --pypi "ray[default]" torch`.
* It must be on storage your cluster sees (see
  [Data and results](#data-and-results)).

## Data and results

Your cluster sees `/work`, `/depot/cms`, `/eos` and `/cvmfs` at the same paths
as your session, with your permissions, so your data paths work unchanged.
Your home directory is not there.

A function can return its results, as above, or write them to `/work`, where
your session reads them like any file.

Ray Train writes a training's checkpoints and results under
`RunConfig(storage_path=...)`, which must be on storage every pod of your
cluster sees, such as your directory on `/work`:
`/work/users/<username>/ray_results`. They stay there after your cluster is
removed. The default, `~/ray_results`, is inside each pod, and with it a
training stops at its first checkpoint with *Unable to set up cluster
storage*. `result.checkpoint.path` is the last checkpoint's directory, which
your session reads like any other.

## Good to know

* You have one cluster, so calls that ask for GPUs share its GPUs, and a call
  that asks for more GPUs than one pod holds never starts.
* Your cluster runs one environment and one number of GPUs at a time.
  Connecting, or submitting a job, with another of either, the defaults
  included (the global environment, one GPU), replaces the cluster when
  nothing runs on it. While something does, `ray.init` fails with a
  connection timeout, and a submission with the reason.
* A cluster with nothing running for some minutes is removed, with the records
  and logs of its jobs: have a job write what you keep to `/work`. A notebook
  still connected to it then fails its next call with a disconnection error:
  run `ray.shutdown()` and `ray.init(...)` again, which starts a new cluster.
