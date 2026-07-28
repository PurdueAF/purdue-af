#!/usr/bin/env python3
"""Run the IRIS-HEP CMS integration challenge as a facility smoke test.

The closest thing this repo has to "a real user doing real analysis": a
coffea Z' -> ttbar workflow over CMS OpenData, executed with the AF's own
`pixi/global` environment. It exercises what no other test here touches —
that the environment we ship can actually preprocess, skim, select and
histogram NanoAOD end to end.

Follows upstream's `full_run_with_metrics.ipynb`, calling the same library
entry points including the roastcoffea `MetricsCollector` layer and the dask
profile capture, so the global env's metrics tooling is covered alongside the
analysis itself. A script rather than the notebook: the notebooks hardcode
their facility (`AF="coffeacasa-condor"`, `REDIRECTOR="root://xcache/"`,
`n_workers=800`) and target `example_cms`, and carry no papermill parameters
to override any of it.

Dask runs on a local cluster, which exercises the part that actually breaks:
the coffea task graph and the cloudpickle-by-value shipping of `intccms`,
`roastcoffea` and the config module to workers.

Usage:

    python run_challenge.py --challenge-root /path/to/integration-challenge/cms
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

# One process keeps the run CI-sized. Measured with --max-files 1 on two
# workers: `signal` resolves to a single ~1.5 GB / 511k-event file, 6s to
# preprocess and ~83s to process, reading straight from eospublic. (Without
# the MetricsCollector the same run processes in ~54s — worker tracking is
# not free, and it is the price of covering the metrics path.)
#
# Do not assume a specific file: `max_files` slices whichever order
# collect_file_paths() returns after aggregating every *.txt in the listing
# directory, so the file (and the runtime) can change when upstream edits a
# listing. Adding processes is a real runtime decision, not a free flag.
DEFAULT_PROCESSES = ["signal"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--challenge-root",
        type=Path,
        required=True,
        help="Path to the `cms/` directory of a checkout of the challenge repo",
    )
    p.add_argument(
        "--processes",
        nargs="+",
        default=DEFAULT_PROCESSES,
        help=f"Physics processes to run (default: {' '.join(DEFAULT_PROCESSES)})",
    )
    p.add_argument(
        "--max-files",
        type=int,
        default=1,
        help="Files per dataset key (default: 1)",
    )
    p.add_argument(
        "--chunksize",
        type=int,
        default=100_000,
        help="Events per coffea chunk (default: 100000)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Dask workers; 0 means one per two available CPUs",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/tmp/integration-challenge"),
        help="Where outputs and the metadata cache go",
    )
    return p.parse_args(argv)


@contextmanager
def dask_client(workers: int):
    """Yield a client for a local dask cluster.

    Processes, not threads: worker-side deserialization of the config module
    is part of what this test is checking, and a threaded cluster would share
    the driver's interpreter and skip it entirely.
    """
    from dask.distributed import Client, LocalCluster

    n_workers = workers or max(1, (os.cpu_count() or 2) // 2)
    cluster = LocalCluster(
        n_workers=n_workers,
        threads_per_worker=1,
        processes=True,
        dashboard_address=None,
    )
    client = Client(cluster)
    print(f"local dask cluster: {n_workers} worker(s)", flush=True)
    try:
        yield client
    finally:
        client.close()
        cluster.close()


def build_config(args: argparse.Namespace) -> object:
    """Load the OpenData config and trim it to a CI-sized run."""
    from example_opendata.configs.configuration import config as upstream_config
    from intccms.schema import Config, load_config_with_restricted_cli

    config = copy.deepcopy(upstream_config)

    general = config["general"]
    general["output_dir"] = str(args.output_dir / "outputs")
    general["cache_dir"] = str(args.output_dir / "cache")
    general["processes"] = list(args.processes)
    # Preprocess fresh every run: a stale cache would let this pass without
    # ever opening a file, which is precisely what we are testing.
    general["run_metadata_generation"] = True
    general["read_from_cache"] = False
    # Skim on the fly, no skim files on disk.
    general["run_processor"] = True
    general["run_analysis"] = True
    general["save_skimmed_output"] = False
    general["run_histogramming"] = True
    # The cabinetry fit, plotting and MVA training cost far more than they
    # prove about whether the facility runs analysis.
    general["run_statistics"] = False
    general["run_plots_only"] = False
    general["run_mva_training"] = False
    # Corrections and systematics wait on an upstream migration. As of
    # 2026-07-27 `example_opendata` still carries the previous schema in two
    # places, each fatal at a different stage:
    #
    #   corrections  spelled `use`/`transform`; CorrectionConfig now wants an
    #                explicit `args` list (ObjVar/Sys/literals)
    #                → pydantic ValidationError at Config(**...)
    #   systematics  SystematicConfig entries; cms.py now demands
    #                uncertainty_sources on CorrectionConfig
    #                → NotImplementedError inside the processor
    #
    # `example_cms` stays clear of both by building them from a CMS-internal
    # directory absent from the repo and falling back to empty. Emptying the
    # blocks is what it takes here: Config validates `corrections` whatever
    # the run_* flags say, and the processor inspects `config.systematics`
    # the same way. Re-enable by deleting these four lines once upstream
    # ports example_opendata forward — that restores correctionlib (pileup +
    # muon SF) and the systematic-variation machinery to this run.
    general["run_corrections"] = False
    general["run_systematics"] = False
    config["corrections"] = []
    config["systematics"] = []

    config["datasets"]["max_files"] = args.max_files

    validated = Config(**load_config_with_restricted_cli(config, []))

    # The dataset listings already carry full root://eospublic.cern.ch/ URLs,
    # so no redirector. (The notebook's "root://xcache/" only resolves inside
    # coffea-casa.)
    for dataset in validated.datasets.datasets:
        dataset.redirector = ""

    return validated


def run(args: argparse.Namespace) -> int:
    import cloudpickle
    import example_opendata
    import intccms
    import roastcoffea
    from coffea.nanoevents import NanoAODSchema
    from coffea.processor import DaskExecutor
    from intccms.analysis import run_processor_workflow
    from intccms.analysis.processors import SkimAndAnalyseProcessor
    from intccms.datasets import DatasetManager
    from intccms.metadata_extractor import DatasetMetadataManager
    from intccms.utils.output import OutputDirectoryManager
    from roastcoffea import MetricsCollector

    # Neither package is installed on the workers; ship them by value.
    cloudpickle.register_pickle_by_value(intccms)
    cloudpickle.register_pickle_by_value(example_opendata)
    cloudpickle.register_pickle_by_value(roastcoffea)

    config = build_config(args)
    output_manager = OutputDirectoryManager(
        root_output_dir=config.general.output_dir,
        cache_dir=config.general.cache_dir,
        metadata_dir=config.general.metadata_dir,
        skimmed_dir=config.general.skimmed_dir,
    )
    dataset_manager = DatasetManager(config.datasets)
    metadata_generator = DatasetMetadataManager(
        dataset_manager=dataset_manager,
        output_manager=output_manager,
        config=config,
        chunksize=args.chunksize,
    )

    with dask_client(args.workers) as client:
        t0 = time.perf_counter()
        print("==> preprocessing (coffea metadata)", flush=True)
        metadata_generator.run(executor=DaskExecutor(client=client))
        metadata_lookup = metadata_generator.build_metadata_lookup()
        workitems = metadata_generator.workitems
        t_meta = time.perf_counter() - t0
        print(f"    {len(workitems)} work items in {t_meta:.1f}s", flush=True)

        print("==> processing (skim + analysis + histogramming)", flush=True)
        # Processor built explicitly rather than letting run_processor_workflow
        # default it: MetricsCollector needs the instance to read the per-chunk
        # metrics that intccms' @track_metrics decorators attach to it.
        processor = SkimAndAnalyseProcessor(
            config=config,
            output_manager=output_manager,
            metadata_lookup=metadata_lookup,
        )
        t1 = time.perf_counter()
        with MetricsCollector(
            client=client,
            processor_instance=processor,
            track_workers=True,
            worker_tracking_interval=1.0,
        ) as collector:
            output, coffea_report = run_processor_workflow(
                config=config,
                output_manager=output_manager,
                metadata_lookup=metadata_lookup,
                processor=processor,
                workitems=workitems,
                executor=DaskExecutor(client=client, treereduction=8, retries=0),
                schema=NanoAODSchema,
                chunksize=args.chunksize,
            )
            collector.extract_metrics_from_output(output)
            collector.set_coffea_report(coffea_report)
        t_proc = time.perf_counter() - t1

        # Best-effort, exactly as upstream's notebook treats it: profile
        # capture depends on workers having been sampled at the right moment
        # and is not worth failing an analysis run over.
        profile_dir = Path(output_manager.root_output_dir) / "profiling"
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            client.profile(filename=str(profile_dir / "dask_profile.html"))
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            print(f"    dask profile capture failed: {exc}", flush=True)

        collector.save_measurement(
            str(Path(output_manager.root_output_dir) / "measurements")
        )
        metrics = collector.get_metrics()

    return report(output, metrics, t_meta, t_proc, config)


def report(
    output: dict, metrics: object, t_meta: float, t_proc: float, config: object
) -> int:
    """Print a summary and decide pass/fail.

    A green run means events were read from CERN, survived selection, and
    landed in a histogram — an empty accumulator with no exception is a
    failure, not a pass.
    """
    from rich.console import Console
    from roastcoffea.export.reporter import (
        format_event_processing_table,
        format_throughput_table,
    )

    processed = output.get("processed_events", 0)
    histograms = output.get("histograms", {})

    console = Console()
    console.print(format_throughput_table(metrics))
    console.print(format_event_processing_table(metrics))

    print()
    print(f"preprocessing : {t_meta:.1f}s")
    print(f"processing    : {t_proc:.1f}s")
    print(f"events read   : {processed:,}")

    failures = []
    if processed <= 0:
        failures.append("no events were processed")
    if not histograms:
        failures.append("no histograms were produced")
    if not metrics:
        failures.append("roastcoffea collected no metrics")

    for channel in config.channels:
        observable = channel.fit_observable
        hist = histograms.get(channel.name, {}).get(observable)
        if hist is None:
            failures.append(f"missing histogram {channel.name}/{observable}")
            continue
        entries = float(hist.sum(flow=True).value)
        print(f"histogram     : {channel.name}/{observable} sum={entries:.4g}")
        if entries <= 0:
            failures.append(f"histogram {channel.name}/{observable} is empty")

    if failures:
        print()
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print()
    print("PASS: the global env ran the integration challenge end to end")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    root = args.challenge_root.resolve()
    if not (root / "example_opendata").is_dir():
        print(f"error: {root} is not the challenge `cms/` directory", file=sys.stderr)
        return 2

    # The configs resolve corrections and dataset listings relative to the
    # working directory ("./example_opendata/..."), so the challenge root has
    # to be the cwd. `intccms` is not installed — it is imported from the
    # checkout, the same way upstream's own notebook does it.
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "src"))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
