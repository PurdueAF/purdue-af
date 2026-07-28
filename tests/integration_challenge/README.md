# Integration challenge

Runs the [IRIS-HEP CMS integration challenge](https://github.com/iris-hep/integration-challenge/tree/main/cms)
over CMS OpenData inside the AF image, using the `pixi/global` environment —
the closest thing here to a real user doing analysis.

Driven by [`run_challenge.py`](run_challenge.py) from
[`ci-integration-challenge.yml`](../../.github/workflows/ci-integration-challenge.yml),
which follows upstream's `full_run_with_metrics.ipynb`.

Runs on manual dispatch and weekly, on its own schedule independent of
`ci.yml`. Keeping it off the per-PR path is what lets it reuse the timing of
`ci-pixi-global.yml`'s expensive env install rather than competing with it.

Covers the AF image, the global env, XRootD reads from `eospublic.cern.ch`
(no grid cert needed), the coffea/dask task graph, skim, cuts, histogramming,
and the roastcoffea metrics layer including the dask profile.

Trimmed to one process at `max_files=1`: ~500k events, about a minute and a
half of processing. Job wall time is dominated by the env install and image
pull.

Tracks upstream `main`; the `ref` dispatch input overrides it. Each run
records the resolved SHA in its job summary — with a moving ref, that is how
you tell "our env drifted" from "upstream changed".

Corrections and systematics await an upstream migration of `example_opendata`
to the schema its own framework enforces; see the comment in
`run_challenge.py` for the lines to re-enable once it lands.

Locally, with the global env installed:

```bash
python tests/integration_challenge/run_challenge.py --challenge-root path/to/integration-challenge/cms
```
