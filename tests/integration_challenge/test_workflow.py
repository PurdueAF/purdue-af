"""Tests for workflows/integration-challenge/workflow.py with flyte and the
analysis stack stubbed: config overrides, the gateway cluster request, the
checkout cache, cluster teardown and the Result mapping."""

import io
import sys
import tarfile
import types
from pathlib import Path

import pytest
from common import REPO, load_script

WORKFLOW = REPO / "workflows" / "integration-challenge" / "workflow.py"


class _TaskEnvironment:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def task(self, fn=None, **kwargs):
        return fn if fn is not None else (lambda f: f)


class _Dir:
    def __init__(self, path):
        self.path = path

    @classmethod
    def from_local_sync(cls, path):
        return cls(path)

    def download_sync(self, dest):
        return str(dest)


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    return mod


def _stub(monkeypatch, name, **attrs):
    mod = _module(name, **attrs)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


@pytest.fixture()
def wf(monkeypatch, tmp_path):
    flyte = _stub(
        monkeypatch,
        "flyte",
        TaskEnvironment=_TaskEnvironment,
        Resources=dict,
        Cache=dict,
        ctx=lambda: None,
    )
    flyte.io = _stub(monkeypatch, "flyte.io", Dir=_Dir)
    mod = load_script(WORKFLOW, "ic_workflow")
    monkeypatch.setattr(mod, "WORKDIR", tmp_path / "ic")
    return mod


def test_task_environment_uses_the_pixi_project(wf):
    assert wf.env.kwargs["env_vars"]["PATH"].startswith(
        f"{wf.PIXI_PROJECT}/.pixi/envs/default/bin:"
    )
    assert wf.Dataset().ic_ref == wf.IC_REF


class TestConfig:
    @pytest.fixture()
    def upstream(self, monkeypatch):
        config = {"general": {"output_dir": "/upstream"}, "datasets": {}}
        _stub(monkeypatch, "example_cms")
        _stub(monkeypatch, "example_cms.configs")
        _stub(monkeypatch, "example_cms.configs.configuration", config=config)

        class Config:
            def __init__(self, **kwargs):
                self.raw = kwargs
                self.datasets = types.SimpleNamespace(
                    datasets=[types.SimpleNamespace(redirector="root://xcache/")] * 2
                )

        def load(config, cli_args):
            assert cli_args == []
            return config

        _stub(monkeypatch, "intccms")
        _stub(
            monkeypatch,
            "intccms.schema",
            Config=Config,
            load_config_with_restricted_cli=load,
        )
        return config

    def test_generates_metadata_when_none_is_given(self, wf, upstream):
        dataset = wf.Dataset(max_files=3, processes=["signal"], redirector="root://r/")
        validated = wf._config(dataset, None)
        general = validated.raw["general"]
        assert general["output_dir"] == str(wf.WORKDIR / "outputs")
        assert general["run_metadata_generation"] is True
        assert general["processes"] == ["signal"]
        assert not (
            general["save_skimmed_output"]
            or general["run_systematics"]
            or general["run_statistics"]
        )
        assert validated.raw["datasets"]["max_files"] == 3
        assert {d.redirector for d in validated.datasets.datasets} == {"root://r/"}
        # upstream module config stays pristine for the next call
        assert upstream == {"general": {"output_dir": "/upstream"}, "datasets": {}}

    def test_reuses_given_metadata(self, wf, upstream):
        general = wf._config(wf.Dataset(), "/meta").raw["general"]
        assert general["metadata_dir"] == "/meta"
        assert general["run_metadata_generation"] is False


def test_metadata_managers_share_the_config_dirs(wf, monkeypatch):
    _stub(monkeypatch, "intccms.datasets", DatasetManager=lambda ds: ("dm", ds))
    _stub(
        monkeypatch,
        "intccms.metadata_extractor",
        DatasetMetadataManager=lambda **kw: kw,
    )
    _stub(monkeypatch, "intccms.utils")
    _stub(monkeypatch, "intccms.utils.output", OutputDirectoryManager=lambda **kw: kw)
    general = types.SimpleNamespace(output_dir="/o", cache_dir="/c", metadata_dir="/m")
    config = types.SimpleNamespace(general=general, datasets="datasets")

    outputs, generator = wf._metadata(wf.Dataset(chunksize=5), config)
    assert outputs == {"root_output_dir": "/o", "cache_dir": "/c", "metadata_dir": "/m"}
    assert generator == {
        "dataset_manager": ("dm", "datasets"),
        "output_manager": outputs,
        "config": config,
        "chunksize": 5,
    }


def test_cluster_request(wf, monkeypatch):
    seen = {}

    class Cluster:
        def scale(self, n):
            seen["scale"] = n

    class Gateway:
        def __init__(self, address, proxy_address, auth):
            seen.update(address=address, proxy=proxy_address, auth=auth)

        def new_cluster(self, **options):
            seen["options"] = options
            return Cluster()

    _stub(monkeypatch, "dask_gateway", Gateway=Gateway)
    _stub(monkeypatch, "dask_gateway.auth", BasicAuth=lambda user: ("basic", user))

    wf._cluster(wf.Cluster(user="u", x509_proxy="/x509", n_workers=7, worker_cores=2))
    assert (seen["address"], seen["proxy"]) == (wf.GATEWAY, wf.GATEWAY_PROXY)
    assert seen["auth"] == ("basic", "u")
    options = seen["options"]
    assert options["pixi_project"] == wf.PIXI_PROJECT
    assert (options["worker_cores"], options["worker_memory"]) == (2, 4)
    assert options["env"]["HOME"] == "/home/u"
    assert options["env"]["X509_USER_PROXY"] == "/x509"
    assert seen["scale"] == 7


class TestCheckout:
    @pytest.fixture()
    def pickled(self, monkeypatch):
        registered = []
        _stub(
            monkeypatch,
            "cloudpickle",
            register_pickle_by_value=lambda m: registered.append(m.__name__),
        )
        _stub(monkeypatch, "example_cms")
        _stub(monkeypatch, "intccms")
        monkeypatch.chdir(Path.cwd())
        monkeypatch.setattr(sys, "path", list(sys.path))
        return registered

    def test_downloads_once_and_prepends_paths(self, wf, monkeypatch, pickled):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("integration-challenge-abc/cms/README")
            tar.addfile(info, io.BytesIO())
        urls = []

        def urlopen(url):
            urls.append(url)
            return io.BytesIO(buf.getvalue())

        monkeypatch.setattr(wf.urllib.request, "urlopen", urlopen)
        root = wf.WORKDIR / "integration-challenge-abc" / "cms"

        wf._checkout("abc")
        assert urls == [f"{wf.IC_REPO}/archive/abc.tar.gz"]
        assert Path.cwd() == root.resolve()
        assert sys.path[:2] == [str(root), str(root / "src")]
        assert sorted(pickled) == ["example_cms", "intccms"]

        wf._checkout("abc")
        assert len(urls) == 1


def _pipeline(wf, monkeypatch, fail=False):
    events = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            events.append("client closed")

        def wait_for_workers(self, n):
            events.append(("wait", n))

        def run(self, fn):
            return {"w1": "2025.1.0", "w2": "2025.1.0", "w3": "2024.9.0"}

    class DaskCluster:
        def get_client(self):
            return Client()

        def shutdown(self):
            events.append("shutdown")

    class Generator:
        workitems = ["item"]

        def run(self, executor=None):
            events.append(("generate", executor))
            if fail and executor is not None:
                raise RuntimeError("xrootd down")

        def build_metadata_lookup(self):
            return {"lookup": 1}

    outputs = types.SimpleNamespace(metadata_dir="/meta-out")
    config = object()
    monkeypatch.setattr(wf, "_checkout", lambda ref: events.append(("checkout", ref)))
    monkeypatch.setattr(
        wf, "_config", lambda ds, meta: events.append(("config", meta)) or config
    )
    monkeypatch.setattr(wf, "_metadata", lambda ds, cfg: (outputs, Generator()))
    monkeypatch.setattr(wf, "_cluster", lambda cluster: DaskCluster())
    _stub(monkeypatch, "coffea")
    _stub(monkeypatch, "coffea.processor", DaskExecutor=lambda **kw: ("dask", kw))
    return events


CLUSTER = dict(user="u", x509_proxy="/x509", n_workers=3)


class TestPreprocess:
    def test_returns_metadata_dir(self, wf, monkeypatch):
        events = _pipeline(wf, monkeypatch)
        result = wf.preprocess(wf.Dataset(ic_ref="r1"), wf.Cluster(**CLUSTER))
        assert result.path == "/meta-out"
        assert events[:2] == [("checkout", "r1"), ("config", None)]
        assert ("wait", 3) in events
        assert events[-1] == "shutdown"

    def test_shuts_the_cluster_down_on_failure(self, wf, monkeypatch):
        events = _pipeline(wf, monkeypatch, fail=True)
        with pytest.raises(RuntimeError, match="xrootd down"):
            wf.preprocess(wf.Dataset(), wf.Cluster(**CLUSTER))
        assert events[-1] == "shutdown"


class TestMeasure:
    @pytest.fixture()
    def analysis(self, wf, monkeypatch):
        seen = {}

        class Collector:
            def __init__(self, **kwargs):
                seen["collector"] = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_metrics_from_output(self, output):
                seen["output"] = output

            def set_coffea_report(self, report):
                seen["report"] = report

            def get_metrics(self):
                return {"data_rate_gbps": 1.5, "processor_cpu_percent": 80.0}

        def run_processor_workflow(**kwargs):
            seen["workflow"] = kwargs
            return {"processed_events": 511_000}, "report"

        _stub(monkeypatch, "coffea.nanoevents", NanoAODSchema="nanoaod")
        _stub(monkeypatch, "roastcoffea", MetricsCollector=Collector)
        _stub(
            monkeypatch,
            "intccms.analysis",
            run_processor_workflow=run_processor_workflow,
        )
        _stub(
            monkeypatch,
            "intccms.analysis.processors",
            SkimAndAnalyseProcessor=lambda **kw: ("processor", kw),
        )
        return seen

    def test_result_mapping(self, wf, monkeypatch, analysis):
        events = _pipeline(wf, monkeypatch)
        monkeypatch.setattr(
            wf.flyte,
            "ctx",
            lambda: types.SimpleNamespace(
                action=types.SimpleNamespace(run_name="run-1")
            ),
        )
        result = wf.measure(
            wf.Dataset(ic_ref="r2"), wf.Cluster(**CLUSTER), _Dir("/remote")
        )
        assert ("config", str(wf.WORKDIR / "metadata")) in events
        assert result.events_processed == 511_000
        assert result.events_skimmed == 0
        assert (result.data_rate_gbps, result.cpu_percent) == (1.5, 80.0)
        assert result.io_wait_percent == 0.0
        assert result.coffea_version == "2024.9.0,2025.1.0"
        assert (result.ic_ref, result.run_name) == ("r2", "run-1")
        assert result.pixi_project == wf.PIXI_PROJECT
        assert result.wall_seconds >= 0
        executor = analysis["workflow"]["executor"]
        assert executor == ("dask", executor[1])
        assert executor[1]["retries"] == 0
        assert analysis["workflow"]["schema"] == "nanoaod"
        assert analysis["workflow"]["workitems"] == ["item"]
        assert analysis["report"] == "report"
        assert events[-1] == "shutdown"

    def test_run_name_without_context(self, wf, monkeypatch, analysis):
        _pipeline(wf, monkeypatch)
        result = wf.measure(wf.Dataset(), wf.Cluster(**CLUSTER), _Dir("/remote"))
        assert result.run_name == ""


def test_benchmark_chains_preprocess_into_measure(wf, monkeypatch):
    monkeypatch.setattr(wf, "preprocess", lambda ds, cl: ("meta", ds.ic_ref))
    monkeypatch.setattr(wf, "measure", lambda ds, cl, meta: (meta, cl.user))
    dataset = wf.Dataset(ic_ref="r3")
    assert wf.benchmark(dataset, wf.Cluster(**CLUSTER)) == (("meta", "r3"), "u")
