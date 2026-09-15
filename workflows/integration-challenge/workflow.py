import copy
import importlib.metadata
import io
import os
import sys
import tarfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import flyte
from flyte.io import Dir

IC_REPO = "https://github.com/iris-hep/integration-challenge"
IC_REF = "f6cd7e1cf6801cad1a743b332c9deed073b70a00"
PIXI_PROJECT = "/work/projects/integration-challenge"
IMAGE = "geddes-registry.rcac.purdue.edu/ghcr-proxy-cache/purdueaf/purdue-af:0.13.4"
GATEWAY = "http://api-dask-gateway-k8s.cms.svc.cluster.local:8000"
GATEWAY_PROXY = "traefik-dask-gateway-k8s.cms.svc.cluster.local:8786"
WORKDIR = Path("/tmp/integration-challenge")

env = flyte.TaskEnvironment(
    name="integration-challenge",
    image=IMAGE,
    resources=flyte.Resources(cpu=2, memory="8Gi"),
    env_vars={"PATH": f"{PIXI_PROJECT}/.pixi/envs/default/bin:/usr/bin:/bin"},
)


@dataclass
class Dataset:
    redirector: str = "root://xcache.cms.rcac.purdue.edu/"
    max_files: int = 1
    processes: list[str] | None = None
    chunksize: int = 200_000
    ic_ref: str = IC_REF


@dataclass
class Cluster:
    user: str
    x509_proxy: str
    n_workers: int = 10
    worker_cores: int = 1
    worker_memory: int = 4


@dataclass
class Result:
    wall_seconds: float
    events_processed: int
    events_skimmed: int
    data_rate_gbps: float
    cpu_percent: float
    io_wait_percent: float
    coffea_version: str
    ic_ref: str
    pixi_project: str
    run_name: str
    executed_at: str


def _checkout(ref: str) -> None:
    root = WORKDIR / f"integration-challenge-{ref}" / "cms"
    if not root.exists():
        WORKDIR.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{IC_REPO}/archive/{ref}.tar.gz") as resp:
            tar = tarfile.open(fileobj=io.BytesIO(resp.read()), mode="r:gz")
            tar.extractall(WORKDIR, filter="data")
    os.chdir(root)
    sys.path[:0] = [str(root), str(root / "src")]

    import cloudpickle
    import example_cms
    import intccms

    cloudpickle.register_pickle_by_value(intccms)
    cloudpickle.register_pickle_by_value(example_cms)


def _config(dataset: Dataset, metadata_dir: str | None) -> Any:
    from example_cms.configs.configuration import config as upstream
    from intccms.schema import Config, load_config_with_restricted_cli

    config = copy.deepcopy(upstream)
    config["general"].update(
        output_dir=str(WORKDIR / "outputs"),
        cache_dir=str(WORKDIR / "cache"),
        metadata_dir=metadata_dir,
        run_metadata_generation=metadata_dir is None,
        processes=dataset.processes,
        save_skimmed_output=False,
        run_systematics=False,
        run_statistics=False,
    )
    config["datasets"]["max_files"] = dataset.max_files
    validated = Config(**load_config_with_restricted_cli(config, []))
    for ds in validated.datasets.datasets:
        ds.redirector = dataset.redirector
    return validated


def _metadata(dataset: Dataset, config: Any) -> tuple[Any, Any]:
    from intccms.datasets import DatasetManager
    from intccms.metadata_extractor import DatasetMetadataManager
    from intccms.utils.output import OutputDirectoryManager

    outputs = OutputDirectoryManager(
        root_output_dir=config.general.output_dir,
        cache_dir=config.general.cache_dir,
        metadata_dir=config.general.metadata_dir,
    )
    generator = DatasetMetadataManager(
        dataset_manager=DatasetManager(config.datasets),
        output_manager=outputs,
        config=config,
        chunksize=dataset.chunksize,
    )
    return outputs, generator


def _cluster(cluster: Cluster) -> Any:
    from dask_gateway import Gateway
    from dask_gateway.auth import BasicAuth

    gateway = Gateway(
        GATEWAY, proxy_address=GATEWAY_PROXY, auth=BasicAuth(cluster.user)
    )
    dask_cluster = gateway.new_cluster(
        pixi_project=PIXI_PROJECT,
        worker_cores=cluster.worker_cores,
        worker_memory=cluster.worker_memory,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": f"/home/{cluster.user}",
            "USER": cluster.user,
            "X509_USER_PROXY": cluster.x509_proxy,
            "X509_CERT_DIR": "/cvmfs/cms.cern.ch/grid/etc/grid-security/certificates",
        },
    )
    dask_cluster.scale(cluster.n_workers)
    return dask_cluster


@env.task(
    cache=flyte.Cache(behavior="auto", ignored_inputs=("cluster",)),
    timeout=timedelta(minutes=30),
)
def preprocess(dataset: Dataset, cluster: Cluster) -> Dir:
    from coffea.processor import DaskExecutor

    _checkout(dataset.ic_ref)
    outputs, generator = _metadata(dataset, _config(dataset, None))
    dask_cluster = _cluster(cluster)
    try:
        with dask_cluster.get_client() as client:
            client.wait_for_workers(cluster.n_workers)
            generator.run(executor=DaskExecutor(client=client))
    finally:
        dask_cluster.shutdown()
    return Dir.from_local_sync(outputs.metadata_dir)


@env.task(timeout=timedelta(hours=1))
def measure(dataset: Dataset, cluster: Cluster, metadata: Dir) -> Result:
    from coffea.nanoevents import NanoAODSchema
    from coffea.processor import DaskExecutor
    from roastcoffea import MetricsCollector

    _checkout(dataset.ic_ref)
    from intccms.analysis import run_processor_workflow
    from intccms.analysis.processors import SkimAndAnalyseProcessor

    config = _config(dataset, metadata.download_sync(WORKDIR / "metadata"))
    outputs, generator = _metadata(dataset, config)
    generator.run()
    lookup = generator.build_metadata_lookup()
    processor = SkimAndAnalyseProcessor(
        config=config, output_manager=outputs, metadata_lookup=lookup
    )

    dask_cluster = _cluster(cluster)
    try:
        with dask_cluster.get_client() as client:
            client.wait_for_workers(cluster.n_workers)
            coffea_versions = set(
                client.run(lambda: importlib.metadata.version("coffea")).values()
            )
            with MetricsCollector(
                client=client,
                processor_instance=processor,
                track_workers=True,
                worker_tracking_interval=1.0,
            ) as collector:
                t0 = time.perf_counter()
                output, report = run_processor_workflow(
                    config=config,
                    output_manager=outputs,
                    metadata_lookup=lookup,
                    processor=processor,
                    workitems=generator.workitems,
                    executor=DaskExecutor(client=client, treereduction=8, retries=0),
                    schema=NanoAODSchema,
                )
                wall = time.perf_counter() - t0
                collector.extract_metrics_from_output(output)
                collector.set_coffea_report(report)
    finally:
        dask_cluster.shutdown()

    metrics = collector.get_metrics()
    ctx = flyte.ctx()
    return Result(
        wall_seconds=wall,
        events_processed=output.get("processed_events", 0),
        events_skimmed=output.get("skimmed_events", 0),
        data_rate_gbps=metrics.get("data_rate_gbps", 0.0),
        cpu_percent=metrics.get("processor_cpu_percent", 0.0),
        io_wait_percent=metrics.get("processor_io_wait_percent", 0.0),
        coffea_version=",".join(sorted(coffea_versions)),
        ic_ref=dataset.ic_ref,
        pixi_project=PIXI_PROJECT,
        run_name=(ctx.action.run_name or "") if ctx else "",
        executed_at=datetime.now(timezone.utc).isoformat(),
    )


@env.task
def benchmark(dataset: Dataset, cluster: Cluster) -> Result:
    result: Result = measure(dataset, cluster, preprocess(dataset, cluster))
    return result


if __name__ == "__main__":
    flyte.init_from_config("config.yaml", root_dir=Path(__file__).parent)
    run = flyte.run(
        benchmark,
        Dataset(),
        Cluster(user=os.environ["USER"], x509_proxy=os.environ["X509_USER_PROXY"]),
    )
    print(run.url)
    run.wait()
    print(run.outputs())
