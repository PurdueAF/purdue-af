# integration-challenge

The [IRIS-HEP integration challenge](https://github.com/iris-hep/integration-challenge)
(CMS Z' → tt̄ over NanoAOD) as a [Flyte 2](https://www.union.ai/docs/v2/flyte/)
workflow on the AF: the control plane is [`apps/flyte`](../../apps/flyte), the
compute is the Geddes Dask Gateway, the data is read from Purdue XCache.

| File           | What it is                                                                 |
| -------------- | -------------------------------------------------------------------------- |
| `workflow.py`  | The tasks, their inputs and the typed result                               |
| `config.yaml`  | Where `flyte` finds the control plane                                      |
| `pixi.toml`    | The environment shared by the task pods and the Dask workers (`pixi.lock`) |

## Tasks

| Task         | Runs                                                        | Cached                                    |
| ------------ | ----------------------------------------------------------- | ----------------------------------------- |
| `preprocess` | coffea preprocessing: file listing, event counts, chunks    | yes, keyed on `Dataset` (`Cluster` is ignored) |
| `measure`    | skim + analysis + histogramming with roastcoffea metrics    | no: a benchmark number is never reused    |
| `benchmark`  | `measure(preprocess())`                                      |                                           |

`Result` records what actually ran: the coffea version reported by a worker,
the challenge commit, the environment path and the Flyte run name.

## Environment

The task pods and the Dask workers use the same pixi environment as the launcher,
so it must live on `/work`, where both can reach it:

```bash
cp pixi.toml pixi.lock /work/users/$USER/integration-challenge/
PIXI_CACHE_DIR=/tmp/pixi-cache-$USER pixi install --manifest-path /work/users/$USER/integration-challenge/pixi.toml --locked
```

`PIXI_PROJECT` in `workflow.py` is derived from the interpreter that launches the run.
The cache goes to local disk because pixi 0.62.2 cannot keep it on CephFS.

## Running

From a notebook or terminal on the AF, with a valid VOMS proxy:

```bash
export X509_USER_PROXY=/depot/cms/users/$USER/x509up_u$(id -u)
cd workflows/integration-challenge
PY=/work/users/$USER/integration-challenge/.pixi/envs/default/bin
$PY/flyte --config config.yaml create project --id integration-challenge --name integration-challenge
$PY/python workflow.py
```

`Dataset()` reads one file per dataset directory through XCache; `Cluster`
sizes the Dask cluster. Run the same `Dataset` twice and the second
`preprocess` is a cache hit.

```bash
$PY/flyte --config config.yaml get run
$PY/flyte --config config.yaml get logs <run-name>
```
