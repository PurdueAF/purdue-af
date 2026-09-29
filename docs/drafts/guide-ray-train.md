# Training on GPUs with Ray

[Ray](https://docs.ray.io/en/latest/ray-core/walkthrough.html) runs a
function of your notebook on a GPU that is yours only while your code needs
it. Your session does not need a GPU of its own: you add a decorator to your
PyTorch training function, and calling it runs it on the GPU.

The function runs in a Ray cluster of your own, with one **NVIDIA T4** GPU
(16 GB). It starts when your notebook first connects, runs as you, sees your
storage at the same paths as your session, and is removed after some minutes
without work. No other user can see, stop or reach it.

## 1. Connect

In a notebook on the **Python (pixi global)** kernel:

```python
import ray

ray.init("ray://ray-train-gateway:10001")
```

The first connection starts your cluster, which takes a minute or two when a
T4 is free; when none is, `ray.init` waits for one.

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
* Your cluster runs one environment at a time. Connecting with another one,
  the global one included, replaces the cluster when nothing runs on it; while
  something does, `ray.init` fails with a connection timeout.

## Data and results

Your cluster sees `/work`, `/depot/cms`, `/eos` and `/cvmfs` at the same paths
as your session, with your permissions, so your data paths work unchanged.
Your home directory is not there.

A function can return its results, as above, or write them to `/work`, where
your session reads them like any file.

## Good to know

* You have one cluster, with one GPU: calls that ask for a GPU run one at a
  time, and a call that asks for more than one never starts.
* A cluster with nothing running for some minutes is removed. A notebook still
  connected to it then fails its next call with a disconnection error: run
  `ray.shutdown()` and `ray.init(...)` again, which starts a new cluster.
