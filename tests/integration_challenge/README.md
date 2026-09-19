# Integration challenge

Runs the [IRIS-HEP CMS integration challenge](https://github.com/iris-hep/integration-challenge/tree/main/cms)
over CMS OpenData inside the AF image, using the `pixi/global` environment —
the closest thing here to a real user doing analysis.

[`run_challenge.py`](run_challenge.py) follows upstream's
`full_run_with_metrics.ipynb`. It runs as a step in stage 1c,
[`ci-pixi-global.yml`](../../.github/workflows/ci-pixi-global.yml).

Covers the AF image, the global env, XRootD reads from `eospublic.cern.ch`
(no grid cert needed), the coffea/dask task graph over a local cluster, skim,
cuts, histogramming, and the roastcoffea metrics layer. One process at
`max_files=1`: ~500k events, about 90 s.

[`upstream.pin`](upstream.pin) fixes the challenge commit. The same challenge
runs on the cluster as a Flyte workflow:
[`workflows/integration-challenge`](../../workflows/integration-challenge/README.md).

Corrections and systematics are off, and their config blocks emptied, in
`run_challenge.py`: upstream's `example_opendata` spells both in a schema its
own framework rejects (a pydantic `ValidationError` for corrections, a
`NotImplementedError` in the processor for systematics).

Locally, with the global env installed:

```bash
python tests/integration_challenge/run_challenge.py --challenge-root path/to/integration-challenge/cms
```
