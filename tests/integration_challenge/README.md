# Integration challenge

Runs the [IRIS-HEP CMS integration challenge](https://github.com/iris-hep/integration-challenge/tree/main/cms)
over CMS OpenData inside the AF image, using the `pixi/global` environment —
the closest thing here to a real user doing analysis. Where the import-smoke
proves the environment resolves, this proves it does physics.

[`run_challenge.py`](run_challenge.py) follows upstream's
`full_run_with_metrics.ipynb`. It runs as a step in stage 1c,
[`ci-pixi-global.yml`](../../.github/workflows/ci-pixi-global.yml).

Covers the AF image, the global env, XRootD reads from `eospublic.cern.ch`
(no grid cert needed), the coffea/dask task graph over a local cluster, skim,
cuts, histogramming, and the roastcoffea metrics layer. One process at
`max_files=1`: ~500k events, about 90 s.

[`upstream.pin`](upstream.pin) fixes the challenge commit; Renovate bumps it,
so upstream movement arrives as its own PR.

Corrections and systematics await an upstream migration of `example_opendata`
to the schema its own framework enforces; see the comment in
`run_challenge.py` for the lines to re-enable once it lands.

Locally, with the global env installed:

```bash
python tests/integration_challenge/run_challenge.py --challenge-root path/to/integration-challenge/cms
```
