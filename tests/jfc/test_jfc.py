"""workflows/jfc against a stub `flyte` (the SDK is not a test dependency), its
wiring to apps/jfc, and the analyses beside it. Git, pixi, the download and the
agent are replaced per test."""

import fcntl
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import types
from pathlib import Path

import pytest
import yaml
from common import REPO, load_script

APP = REPO / "apps" / "jfc"
WORKFLOW = REPO / "workflows" / "jfc"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"


class Launcher:
    """What the launcher asks of the control plane, recorded."""

    projects: list[str] = []
    runs: list[dict] = []

    @classmethod
    def listall(cls):
        return [
            types.SimpleNamespace(pb2=types.SimpleNamespace(id=p)) for p in cls.projects
        ]

    @classmethod
    def create(cls, id, name):
        cls.projects.append(id)

    @classmethod
    def with_runcontext(cls, name):
        def run(task, **inputs):
            cls.runs.append({"name": name, "task": task, **inputs})
            return types.SimpleNamespace(name=name, url="url")

        return types.SimpleNamespace(run=run)


def _load():
    flyte = types.ModuleType("flyte")
    flyte.TaskEnvironment = lambda **options: types.SimpleNamespace(
        options=options, task=lambda **task: lambda fn: fn
    )
    flyte.Resources = lambda **kw: kw
    flyte.init_from_config = lambda *args, **kw: None
    flyte.with_runcontext = Launcher.with_runcontext
    flyte.errors = types.ModuleType("flyte.errors")
    flyte.errors.NonRecoverableError = type("NonRecoverable", (RuntimeError,), {})
    flyte.remote = types.ModuleType("flyte.remote")
    flyte.remote.Project = Launcher
    stubs = {"flyte": flyte, "flyte.errors": flyte.errors, "flyte.remote": flyte.remote}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    # The launcher and the task find jfc_profiling beside jfc.py.
    sys.path.insert(0, str(WORKFLOW))
    try:
        return load_script(WORKFLOW / "jfc.py", "jfc_flows"), stubs
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


jfc, STUBS = _load()
profiling = jfc.jfc_profiling
clusters = jfc.jfc_dask
gpus = jfc.jfc_ray

ANALYSIS = jfc.Analysis(
    name="zz",
    kind="search",
    repo="https://example.org/zz.git",
    ref="c0ffee",
    files=["docs", "ntuplize.py"],
    data="https://example.org/zz.tgz",
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(jfc, "ROOT", tmp_path)
    return tmp_path


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


# --- wiring ---


def test_flux_lists_every_manifest_of_the_app():
    resources = yaml.safe_load(EXPERIMENTAL.read_text())["resources"]
    assert {r for r in resources if "/apps/jfc/" in r} == {
        f"../../apps/jfc/{path.name}" for path in APP.glob("*.yaml")
    }


def test_the_task_runs_in_the_pod_template_and_project_named_after_the_workflow():
    (template,) = docs(APP / "podtemplate.yaml")
    config = yaml.safe_load((WORKFLOW / "config.yaml").read_text())
    flyte_values = yaml.safe_load((REPO / "apps/flyte/values.yaml").read_text())
    assert jfc.env.options["pod_template"] == template["metadata"]["name"]
    assert config["task"]["project"] == jfc.NAME
    assert f"{flyte_values['fullnameOverride']}-http." in config["admin"]["endpoint"]


def test_task_pods_mount_only_the_directory_the_workspace_job_creates_for_them():
    (template,) = docs(APP / "podtemplate.yaml")
    (job,) = docs(APP / "workspace.yaml")
    pod = template["template"]["spec"]
    mounts = pod["containers"][0]["volumeMounts"]
    (mount,) = [m for m in mounts if m["mountPath"].startswith("/work")]
    assert mount["mountPath"] == str(jfc.ROOT) == f"/work/{mount['subPath']}"

    container = job["spec"]["template"]["spec"]["containers"][0]
    (made,) = container["volumeMounts"]
    directory = f"{made['mountPath']}/{jfc.NAME}"
    owner = "{runAsUser}:{runAsGroup}".format(**pod["securityContext"])
    assert f"{made['subPath']}/{jfc.NAME}" == mount["subPath"]
    assert container["command"][-1] == (
        f"mkdir -p {directory} && chown {owner} {directory}"
    )


def _found(document, key):
    """The value of the first `key` anywhere in a parsed YAML document."""
    if isinstance(document, dict):
        if key in document:
            return document[key]
        document = list(document.values())
    if isinstance(document, list):
        for item in document:
            if (value := _found(item, key)) is not None:
                return value
    return None


def test_task_pods_send_profiles_where_the_sessions_do_as_the_workflow():
    (template,) = docs(APP / "podtemplate.yaml")
    container = template["template"]["spec"]["containers"][0]
    env = {variable["name"]: variable["value"] for variable in container["env"]}
    hub = yaml.safe_load((REPO / "apps/jupyterhub/jupyterhub/values.yaml").read_text())
    assert env["PYROSCOPE_SERVER"] == _found(hub, "PYROSCOPE_SERVER")
    assert env["PYROSCOPE_APP"] == jfc.NAME


def test_task_pods_reach_the_sessions_gateway_as_the_sessions_do():
    (template,) = docs(APP / "podtemplate.yaml")
    container = template["template"]["spec"]["containers"][0]
    env = {variable["name"]: variable["value"] for variable in container["env"]}
    spawner = (
        REPO / "apps/jupyterhub/jupyterhub/extraFiles/custom-spawner.py"
    ).read_text()
    names = [name for name in env if name.startswith("DASK_GATEWAY__")]
    assert len(names) == 3
    for name in names:
        assert re.search(rf'"{name}",\s*"{re.escape(env[name])}"', spawner)


def test_task_pods_hold_the_facilitys_proxy_and_the_certificates_to_use_it():
    (template,) = docs(APP / "podtemplate.yaml")
    pod = template["template"]["spec"]
    (container,) = pod["containers"]
    env = {variable["name"]: variable["value"] for variable in container["env"]}
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    volumes = {v["name"]: v for v in pod["volumes"]}
    flux = yaml.safe_load((EXPERIMENTAL.parent / "flux-kustomization.yaml").read_text())
    names = flux["spec"]["postBuild"]["substitute"]

    (proxy,) = [n for n, v in volumes.items() if "secret" in v]
    secret = volumes[proxy]["secret"]
    assert secret["secretName"] == "${x509_secret_name}" and names["x509_secret_name"]
    # Readable by the task user, whom the Secret's files do not belong to.
    assert secret["defaultMode"] & 0o004
    assert env["X509_USER_PROXY"] == f"{mounts[proxy]['mountPath']}/x509up"
    (cvmfs,) = [
        n
        for n, v in volumes.items()
        if v.get("persistentVolumeClaim", {}).get("claimName") == "cvmfs"
    ]
    assert env["X509_CERT_DIR"].startswith(f"{mounts[cvmfs]['mountPath']}/")
    assert mounts[cvmfs]["readOnly"] and mounts[proxy]["readOnly"]


def test_the_credentials_secret_is_optional_so_a_task_can_say_it_is_missing():
    (template,) = docs(APP / "podtemplate.yaml")
    (source,) = template["template"]["spec"]["containers"][0]["envFrom"]
    assert source["secretRef"]["optional"] is True


# --- the launcher ---


@pytest.fixture
def launched(tmp_path, monkeypatch):
    for name, module in STUBS.items():
        monkeypatch.setitem(sys.modules, name, module)
    (tmp_path / "analysis.toml").write_text(
        'name = "zz"\nkind = "search"\nrepo = "https://example.org/zz.git"\nref = "c0ffee"\n'
    )
    Launcher.projects[:] = []
    Launcher.runs[:] = []
    return str(tmp_path)


def test_every_analysis_directory_launches_a_run_on_inputs_it_pins(launched):
    described = sorted((REPO / "workflows").glob("jfc-*/analysis.toml"))
    assert described
    for path in described:
        jfc.launch(str(path.parent))
        run = Launcher.runs[-1]
        analysis = run["analysis"]
        assert path.parent.name == f"jfc-{analysis.name}"
        assert analysis.kind in ("measurement", "search")
        if analysis.repo:
            assert re.fullmatch(r"[0-9a-f]{40}", analysis.ref)
        else:
            # The run carries the files themselves.
            for name in ("prompt.md", *analysis.files):
                assert any(
                    sent == name or sent.startswith(f"{name}/")
                    for sent in analysis.sources
                )
        assert len(run["name"]) <= 30


def test_an_analysis_without_a_repo_sends_its_prompt_and_files(launched):
    directory = Path(launched)
    (directory / "analysis.toml").write_text(
        'name = "zz"\nkind = "search"\nfiles = ["datasets.yaml", "notes"]\n'
    )
    (directory / "prompt.md").write_text("search for ZZ")
    (directory / "datasets.yaml").write_text("2018: []")
    (directory / "notes").mkdir()
    (directory / "notes" / "trigger.md").write_text("IsoMu24")
    (directory / "README.md").write_text("not sent")
    (directory / "notes" / ".DS_Store").write_bytes(b"\x00\x87")

    jfc.launch(launched)

    (run,) = Launcher.runs
    assert run["analysis"].sources == {
        "prompt.md": "search for ZZ",
        "datasets.yaml": "2018: []",
        "notes/trigger.md": "IsoMu24",
    }


def test_an_analysis_naming_a_file_it_lacks_does_not_launch(launched):
    directory = Path(launched)
    (directory / "analysis.toml").write_text(
        'name = "zz"\nkind = "search"\nfiles = ["datasets.yaml"]\n'
    )
    (directory / "prompt.md").write_text("search for ZZ")

    with pytest.raises(FileNotFoundError, match="datasets.yaml"):
        jfc.launch(launched)
    assert Launcher.runs == []


def test_launch_starts_a_run_named_after_the_analysis_in_a_workspace_of_its_own(
    launched,
):
    jfc.launch(launched)
    jfc.launch(launched)

    first, _ = Launcher.runs
    assert first["task"] is jfc.advance
    assert first["analysis"] == jfc.Analysis(
        "zz", "search", "https://example.org/zz.git", "c0ffee"
    )
    assert first["name"].startswith("jfc-zz-") and first["workspace"] == first["name"]
    assert first["reply"] == ""
    assert Launcher.projects == [jfc.NAME]


def test_launch_carries_a_reply_whole_to_the_workspace_it_names(launched):
    jfc.launch(launched, "jfc-zz-1013-120000", "ITERATE:", "redo the fit")

    (run,) = Launcher.runs
    assert run["workspace"] == "jfc-zz-1013-120000"
    assert run["reply"] == "ITERATE: redo the fit"


# --- setup ---


def _fake_tools(monkeypatch, data):
    """git and pixi as recorded calls; the scaffolder as the directory it makes."""
    calls = []

    def checkout(source, dest):
        calls.append(("checkout", dest.name, *source))
        (dest / "docs").mkdir(parents=True)
        (dest / "docs" / "paper.pdf").write_text("paper")
        (dest / "ntuplize.py").write_text("ntuplizer")
        (dest / "prompt.md").write_text("search for ZZ")

    def run(*command, cwd):
        if command[0] == "pixi":
            calls.append(command)
        else:
            calls.append(command[1:] if command[0] == sys.executable else command[:2])
        if "src/scaffold_analysis.py" in command:
            (cwd / command[2]).mkdir(parents=True)

    monkeypatch.setattr(jfc, "_checkout", checkout)
    monkeypatch.setattr(jfc, "_run", run)
    monkeypatch.setattr(jfc, "_data", lambda analysis: data)
    monkeypatch.setattr(jfc, "_install", lambda work: calls.append(("install", work)))
    return calls


def test_prepare_stages_the_analysis_into_its_scaffold(root, monkeypatch):
    data = root / "data"
    data.mkdir()
    calls = _fake_tools(monkeypatch, data)

    work = jfc._prepare(root / "run", ANALYSIS)

    assert work == root / "run/jfc/analyses/zz"
    assert ("checkout", "jfc", *jfc.JFC) in calls
    assert ("checkout", "source", ANALYSIS.repo, ANALYSIS.ref) in calls
    assert ("src/scaffold_analysis.py", "analyses/zz", "--type", "search") in calls
    assert (work / "data").resolve() == data.resolve()
    assert (work / "prompt.md").read_text() == "search for ZZ"
    assert (work / "docs/paper.pdf").is_file()
    assert (work / "ntuplize.py").is_file()
    assert calls[-3:] == [
        ("pixi", "add", "--no-install", *jfc.CONDA),
        ("pixi", "add", "--pypi", *jfc.PACKAGES),
        ("install", work),
    ]


def test_prepare_stages_an_analysis_that_brings_its_own_files(root, monkeypatch):
    calls = _fake_tools(monkeypatch, root)
    local = jfc.Analysis(
        "zz",
        "search",
        files=["datasets.yaml", "notes"],
        sources={
            "prompt.md": "search for ZZ",
            "datasets.yaml": "2018: []",
            "notes/trigger.md": "IsoMu24",
        },
    )

    work = jfc._prepare(root / "run", local)

    assert [call for call in calls if call[:2] == ("checkout", "source")] == []
    assert (work / "prompt.md").read_text() == "search for ZZ"
    assert (work / "datasets.yaml").read_text() == "2018: []"
    assert (work / "notes/trigger.md").read_text() == "IsoMu24"
    assert not (work / "data").exists()


def test_prepare_gives_the_workers_a_directory_they_can_write(root, monkeypatch):
    _fake_tools(monkeypatch, root)

    jfc._prepare(root / "run", ANALYSIS)

    assert (root / "run/shared").stat().st_mode & 0o7777 == 0o1777


def test_the_environment_gets_the_helpers_and_every_python_starts_the_profiler(
    tmp_path, monkeypatch
):
    packages, seen = tmp_path / "site-packages", tmp_path / "seen.json"
    packages.mkdir()
    python = tmp_path / ".pixi/envs/default/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\necho {packages}\n")
    python.chmod(0o755)
    (packages / "pyroscope.py").write_text(
        "import json, os\n"
        "def configure(**options):\n"
        "    with open(os.environ['SEEN'], 'w') as seen:\n"
        "        json.dump(options, seen)\n"
        "def shutdown():\n"
        "    pass\n"
    )

    jfc._install(tmp_path)
    for module in (profiling, clusters, gpus):
        installed = packages / f"{module.__name__}.py"
        assert installed.read_text() == open(module.__file__).read()
    env = {
        **os.environ,
        "PYROSCOPE_SERVER": "http://pyroscope:4040",
        "PYROSCOPE_APP": "jfc",
        "JFC_ANALYSIS": "zz",
        "JFC_WORKSPACE": "ws",
        "SEEN": str(seen),
    }
    load = f"import site; site.addsitedir({str(packages)!r})"
    subprocess.run([sys.executable, "-c", load], env=env, check=True)

    options = json.loads(seen.read_text())
    assert options["application_name"] == "jfc"
    assert options["server_address"] == "http://pyroscope:4040"
    assert options["tags"] == {"analysis": "zz", "workspace": "ws", "script": "-c"}


@pytest.fixture
def pyroscope(monkeypatch):
    """A stand-in for pyroscope-io, recording how it is started."""
    calls = types.SimpleNamespace(configured=[], at_exit=[])
    module = types.ModuleType("pyroscope")

    # Keywords of pyroscope-io 1.3's configure: it refuses any other.
    def configure(
        *,
        application_name=None,
        server_address="http://localhost:4040",
        enable_logging=False,
        sample_rate=100,
        oncpu=True,
        gil_only=True,
        report_pid=False,
        tags=None,
        upload_interval=10,
        mem_enabled=False,
    ):
        calls.configured.append(dict(locals()))

    module.configure = configure
    module.shutdown = lambda: None
    monkeypatch.setitem(sys.modules, "pyroscope", module)
    monkeypatch.setattr(profiling, "started", False)
    monkeypatch.setattr(profiling.atexit, "register", calls.at_exit.append)
    calls.module = module
    return calls


def test_the_profiler_starts_once_and_only_where_a_server_is_named(
    pyroscope, monkeypatch
):
    monkeypatch.delenv("PYROSCOPE_SERVER", raising=False)
    profiling.start()
    assert pyroscope.configured == []

    monkeypatch.setenv("PYROSCOPE_SERVER", "http://pyroscope:4040")
    profiling.start()
    profiling.start()

    (options,) = pyroscope.configured
    assert options["server_address"] == "http://pyroscope:4040"
    assert options["gil_only"] is False and options["oncpu"] is True
    assert pyroscope.at_exit == [pyroscope.module.shutdown]


@pytest.mark.parametrize("broken", ["missing", "raising"])
def test_a_profiler_that_cannot_start_leaves_the_script_alone(
    pyroscope, monkeypatch, broken
):
    monkeypatch.setenv("PYROSCOPE_SERVER", "http://pyroscope:4040")
    if broken == "missing":
        monkeypatch.setitem(sys.modules, "pyroscope", None)
    else:

        def configure(**options):
            raise OSError("no route to pyroscope")

        pyroscope.module.configure = configure

    profiling.start()

    assert pyroscope.at_exit == []


def test_a_turn_goes_on_when_the_helpers_cannot_be_installed(root, monkeypatch):
    _fake_tools(monkeypatch, root)

    def broken(work):
        raise subprocess.CalledProcessError(1, "python")

    monkeypatch.setattr(jfc, "_install", broken)

    assert jfc._prepare(root / "run", ANALYSIS) == root / "run/jfc/analyses/zz"


def test_prepare_puts_the_scope_of_an_analysis_after_its_prompt(root, monkeypatch):
    _fake_tools(monkeypatch, root)
    narrowed = jfc.Analysis(**{**vars(ANALYSIS), "scope": "Two samples only."})

    work = jfc._prepare(root / "run", narrowed)
    jfc._prepare(root / "run", narrowed)

    assert (work / "prompt.md").read_text() == "search for ZZ\n\nTwo samples only."


def test_prepare_stages_only_the_prompt_of_an_analysis_that_names_nothing_else(
    root, monkeypatch
):
    _fake_tools(monkeypatch, root)
    bare = jfc.Analysis("zz", "search", ANALYSIS.repo, ANALYSIS.ref)

    work = jfc._prepare(root / "run", bare)

    assert sorted(path.name for path in work.iterdir()) == ["prompt.md"]


def test_prepare_leaves_a_staged_workspace_to_the_agent_but_its_helpers(
    root, monkeypatch
):
    calls = _fake_tools(monkeypatch, root)
    work = jfc._prepare(root / "run", ANALYSIS)
    (work / "prompt.md").unlink()
    (work / "experiment_log.md").write_text("the agent's work")
    (root / "run/shared").rmdir()
    calls.clear()

    assert jfc._prepare(root / "run", ANALYSIS) == work

    # The helpers of each turn's rules reach a workspace staged before them.
    assert calls == [("install", work)]
    assert (work / "experiment_log.md").is_file()
    assert (root / "run/shared").stat().st_mode & 0o7777 == 0o1777


def test_prepare_redoes_a_setup_that_died_before_its_last_step(root, monkeypatch):
    calls = _fake_tools(monkeypatch, root)
    run = jfc._run

    def dies_installing(*command, cwd):
        run(*command, cwd=cwd)
        if command[0] == "pixi":
            raise OSError("killed")

    monkeypatch.setattr(jfc, "_run", dies_installing)
    with pytest.raises(OSError):
        jfc._prepare(root / "run", ANALYSIS)
    monkeypatch.setattr(jfc, "_run", run)
    calls.clear()

    work = jfc._prepare(root / "run", ANALYSIS)

    assert [call[:2] for call in calls[:2]] == [
        ("checkout", "jfc"),
        ("checkout", "source"),
    ]
    assert calls[-3:] == [
        ("pixi", "add", "--no-install", *jfc.CONDA),
        ("pixi", "add", "--pypi", *jfc.PACKAGES),
        ("install", work),
    ]


def test_data_is_fetched_once_per_url(root, tmp_path_factory, monkeypatch):
    source = tmp_path_factory.mktemp("s3") / "zz.tgz"
    with tarfile.open(source, "w:gz") as tar:
        info = tarfile.TarInfo("data/ZZTo4L.root")
        info.size = 6
        tar.addfile(info, io.BytesIO(b"events"))
    fetched = []

    def urlretrieve(url, dest):
        fetched.append(url)
        dest.write_bytes(source.read_bytes())
        return dest, None

    monkeypatch.setattr(jfc.urllib.request, "urlretrieve", urlretrieve)

    data = jfc._data(ANALYSIS)
    assert jfc._data(ANALYSIS) == data and data.parent == root / "data"
    assert (data / "ZZTo4L.root").read_bytes() == b"events"
    assert fetched == [ANALYSIS.data]

    moved = jfc.Analysis(
        "zz", "search", ANALYSIS.repo, ANALYSIS.ref, data="https://example.org/v2.tgz"
    )
    assert jfc._data(moved) != data
    assert sorted(path.name for path in (root / "data").iterdir()) == sorted(
        [data.name, jfc._data(moved).name]
    )


# --- the analysis's Dask cluster ---


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """A stand-in for dask_gateway, over the clusters in `running`; the test's
    Python is the default pixi environment of `project`."""
    state = types.SimpleNamespace(
        project=str(tmp_path / "zz"), running=[], started=[], held=[]
    )
    prefix = tmp_path / "zz/.pixi/envs/default"
    prefix.mkdir(parents=True)

    class Cluster:
        def __init__(self, name):
            self.name = name

        def adapt(self, **bounds):
            state.adapted = (self.name, bounds)

        def get_client(self):
            state.client = types.SimpleNamespace(
                of=self.name,
                status="running",
                plugins=[],
                register_plugin=lambda plugin, name: state.client.plugins.append(
                    (name, plugin)
                ),
            )
            return state.client

    class Gateway:
        def list_clusters(self):
            return [
                types.SimpleNamespace(
                    name=name,
                    options={
                        "pixi_project": project,
                        "worker_cores": 1,
                        "worker_memory": 4,
                    },
                )
                for name, project in state.running
            ]

        def connect(self, name):
            return Cluster(name)

        def new_cluster(self, **options):
            with open(prefix / "jfc_dask.lock") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    state.held.append(True)
            state.started.append(options)
            return Cluster("new")

    module = types.ModuleType("dask_gateway")
    module.Gateway = Gateway
    monkeypatch.setitem(sys.modules, "dask_gateway", module)
    monkeypatch.setattr(clusters.sys, "prefix", str(prefix))
    monkeypatch.delenv("X509_USER_PROXY", raising=False)
    return state


def test_a_script_starts_the_analysis_cluster_on_its_environment(gateway, monkeypatch):
    monkeypatch.setenv("PYROSCOPE_SERVER", "http://pyroscope:4040")
    monkeypatch.delenv("PYROSCOPE_APP", raising=False)
    monkeypatch.setenv("JFC_ANALYSIS", "zz")
    monkeypatch.setenv("JFC_WORKSPACE", "ws")
    monkeypatch.setenv("JUPYTERHUB_API_TOKEN", "not for workers")
    monkeypatch.setenv("X509_CERT_DIR", "/cvmfs/certificates")

    assert clusters.client(workers=12, cores=4, memory=16).of == "new"

    assert gateway.started == [
        {
            "pixi_project": gateway.project,
            "pixi_env": "default",
            "worker_cores": 4,
            "worker_memory": 16,
            "env": {
                "PATH": os.environ["PATH"],
                "PYROSCOPE_SERVER": "http://pyroscope:4040",
                "JFC_ANALYSIS": "zz",
                "JFC_WORKSPACE": "ws",
                "X509_CERT_DIR": "/cvmfs/certificates",
            },
            "shutdown_on_close": False,
        }
    ]
    assert gateway.adapted == ("new", {"minimum": 0, "maximum": 12})
    assert gateway.client.plugins == []


def test_a_script_starts_a_cluster_while_no_other_of_the_analysis_can(gateway):
    clusters.client()

    assert gateway.held == [True]


def test_the_analysis_scripts_share_its_running_cluster(gateway):
    gateway.running = [("ours", gateway.project)]

    assert clusters.client().of == "ours"

    assert gateway.started == []
    assert gateway.adapted == ("ours", {"minimum": 0, "maximum": 50})


def test_workers_get_the_pods_proxy_now_and_again_while_the_client_is_open(
    gateway, tmp_path, monkeypatch
):
    proxy = tmp_path / "x509up"
    proxy.write_bytes(b"proxy one")
    monkeypatch.setenv("X509_USER_PROXY", str(proxy))
    tick, ticked = threading.Event(), threading.Event()

    def sleep(seconds):
        assert seconds == clusters.RENEW
        ticked.set()
        tick.wait(5)
        tick.clear()

    monkeypatch.setattr(clusters.time, "sleep", sleep)

    dask = clusters.client()
    assert ticked.wait(5)
    proxy.write_bytes(b"proxy two")
    ticked.clear()
    tick.set()
    assert ticked.wait(5)
    dask.status = "closed"
    tick.set()

    (name, first), (_, renewed) = dask.plugins
    assert name == clusters.Proxy.name
    assert (first.data, renewed.data) == (b"proxy one", b"proxy two")


def test_workers_get_the_proxy_once_the_pod_has_it(gateway, tmp_path, monkeypatch):
    proxy = tmp_path / "x509up"
    monkeypatch.setenv("X509_USER_PROXY", str(proxy))
    monkeypatch.setattr(
        clusters.threading,
        "Thread",
        lambda target, daemon: types.SimpleNamespace(start=lambda: None),
    )

    dask = clusters.client()

    assert dask.plugins == []


def test_a_script_is_told_the_running_cluster_keeps_its_own_workers(gateway):
    gateway.running = [("ours", gateway.project)]

    with pytest.warns(UserWarning, match="1 cores and 4 GiB"):
        clusters.client(cores=4, memory=16)


def test_a_worker_keeps_the_proxy_in_a_file_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(clusters.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("X509_USER_PROXY", raising=False)

    clusters.Proxy(b"proxy").setup(worker=None)

    kept = Path(os.environ.pop("X509_USER_PROXY"))
    assert kept.parent == tmp_path and kept.read_bytes() == b"proxy"
    assert kept.stat().st_mode & 0o777 == 0o600


def test_a_script_waits_for_another_cluster_of_the_user_to_end(gateway):
    gateway.running = [("theirs", "/work/projects/jfc/other")]

    with pytest.raises(RuntimeError, match="theirs is running"):
        clusters.client()

    assert gateway.started == []


# --- the analysis's Ray jobs ---


@pytest.fixture
def ray(monkeypatch):
    """A stand-in for ray.job_submission, recording the clients made."""
    made = []

    def JobSubmissionClient(address, headers):
        made.append({"address": address, "headers": headers, "env": dict(os.environ)})
        return made[-1]

    module = types.ModuleType("ray.job_submission")
    module.JobSubmissionClient = JobSubmissionClient
    monkeypatch.setitem(sys.modules, "ray", types.ModuleType("ray"))
    monkeypatch.setitem(sys.modules, "ray.job_submission", module)
    monkeypatch.setenv("JUPYTERHUB_API_TOKEN", "hub token")
    for name in ("RAY_AUTH_MODE", "RAY_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return made


def test_ray_jobs_run_in_the_environment_on_the_settings_asked_for(ray):
    gpus.client(max_workers=4, gpus_per_worker=2)

    (made,) = ray
    assert made["address"] == gpus.ADDRESS
    assert made["headers"] == {
        "Authorization": "Bearer hub token",
        "af-env": os.path.realpath(sys.prefix),
        "af-max-workers": "4",
        "af-gpus-per-worker": "2",
    }


def test_ray_jobs_may_run_in_another_environment(ray):
    gpus.client(env="/work/projects/jfc/ws/env")

    assert ray[0]["headers"]["af-env"] == "/work/projects/jfc/ws/env"


def test_the_ray_gateway_takes_the_settings_the_helper_sends():
    gateway = (REPO / "apps/ray-train/gateway.py").read_text()
    prefix = re.search(r'^SETTING_PREFIX = "(.+)"$', gateway, re.M).group(1)
    settings = set(re.findall(r'SETTING_PREFIX \+ "([a-z-]+)"', gateway))
    assert prefix == "af-"
    assert {"env", "max-workers", "gpus-per-worker"} <= settings


# --- the agent ---


def _workspace(root):
    work, config = root / "jfc" / "analyses" / "zz", root / "claude"
    work.mkdir(parents=True)
    (work / "prompt.md").write_text("search for ZZ")
    return work, config


def _session(command):
    flag = "--resume" if "--resume" in command else "--session-id"
    return flag, command[command.index(flag) + 1]


def test_first_turn_opens_a_session_with_the_analysis_prompt(root):
    work, config = _workspace(root)

    command = jfc._turn(work, config, "")

    assert command[:2] == ["claude", "--print"]
    assert command[2].endswith("search for ZZ")
    assert _session(command)[0] == "--session-id"
    assert command[command.index("--permission-mode") + 1] == jfc.PERMISSION_MODE
    assert command[command.index("--model") + 1] == jfc.MODEL
    # The scaffold's methodology, agents and conventions link into it.
    assert command[command.index("--add-dir") + 1] == str(root / "jfc" / "src")
    assert "--forward-subagent-text" in command


def test_later_turns_resume_that_session_with_the_reply_or_a_nudge(root):
    work, config = _workspace(root)
    _, session = _session(jfc._turn(work, config, ""))
    (config / "projects/slug").mkdir(parents=True)
    (config / "projects/slug" / f"{session}.jsonl").write_text("{}")

    replied = jfc._turn(work, config, "APPROVE")
    nudged = jfc._turn(work, config, "")

    assert _session(replied) == _session(nudged) == ("--resume", session)
    assert replied[2] == jfc.REPLY.format(reply="APPROVE")
    assert nudged[2] == jfc.INTERRUPTED


def _fake_claude(tmp_path, lines, code=0):
    """A stand-in for the CLI: prints `lines`, then exits with `code`."""
    script = tmp_path / "claude.py"
    script.write_text(
        "import os, sys\n"
        f"for line in {lines!r}:\n"
        "    for name in ('JFC_ANALYSIS', 'JFC_WORKSPACE', 'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS', 'BASH_MAX_TIMEOUT_MS'):\n"
        "        line = line.replace('$' + name, os.environ[name])\n"
        "    print(line.replace('$CONFIG', os.environ['CLAUDE_CONFIG_DIR']))\n"
        f"sys.exit({code})\n"
    )
    return [sys.executable, str(script)]


def test_claude_runs_every_subagent_and_command_in_the_foreground(tmp_path):
    result = {
        "type": "result",
        "is_error": False,
        "result": "$CLAUDE_CODE_DISABLE_BACKGROUND_TASKS $BASH_MAX_TIMEOUT_MS",
    }
    command = _fake_claude(tmp_path, [json.dumps(result)])

    said = jfc._claude(command, tmp_path, tmp_path / "c", tmp_path / "tokens", 0)

    # A foreground command may last as long as a pass over the data.
    assert said == f"1 {jfc.COMMAND_HOURS * 3600 * 1000}"


def test_claude_names_the_analysis_and_workspace_its_scripts_tag_profiles_with(
    tmp_path,
):
    work = tmp_path / "ws" / "jfc" / "analyses" / "zz"
    work.mkdir(parents=True)
    result = {
        "type": "result",
        "is_error": False,
        "result": "$JFC_ANALYSIS $JFC_WORKSPACE",
    }
    command = _fake_claude(tmp_path, [json.dumps(result)])

    said = jfc._claude(command, work, tmp_path / "c", tmp_path / "tokens", 0)

    assert said == "zz ws"


def _assistant(*content, **extra):
    return json.dumps({"type": "assistant", "message": {"content": content}, **extra})


def test_claude_returns_the_result_and_narrates_the_turn(tmp_path, capsys):
    tool = {"type": "tool_use", "name": "Bash", "input": {"command": "pixi run all"}}
    lines = [
        "a warning that is not JSON",
        _assistant(tool),
        _assistant({"type": "text", "text": "fit\nconverged"}, parent_tool_use_id="t"),
        _assistant({"type": "text", "text": "x" * 999}),
        _assistant(parent_tool_use_id="t"),
        json.dumps({"type": "user", "message": {"content": "tool output"}}),
        json.dumps(
            {
                "type": "system",
                "subtype": "api_retry",
                "attempt": 2,
                "max_retries": 10,
                "error": "rate_limit",
            }
        ),
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "gate: $CONFIG",
                "num_turns": 7,
                "modelUsage": {"opus": {"inputTokens": 1200, "costUSD": 1.5}},
            }
        ),
    ]

    (tmp_path / "tokens").write_text("50")

    said = jfc._claude(
        _fake_claude(tmp_path, lines),
        tmp_path,
        tmp_path / "cfg",
        tmp_path / "tokens",
        0,
    )

    assert said == f"gate: {tmp_path / 'cfg'}"
    logged = [line.split("] ", 1)[1] for line in capsys.readouterr().out.splitlines()]
    assert logged == [
        "a warning that is not JSON",
        'Bash {"command": "pixi run all"}',
        "  fit converged",
        "x" * jfc.LOG_LINE,
        "API retry 2/10: rate_limit",
        "turn over: 7 turns, 1200 tokens",
    ]


@pytest.mark.parametrize(
    "result, code",
    [
        ({"type": "result", "is_error": True, "result": "Not logged in"}, 1),
        ({"type": "result", "is_error": False, "result": "done"}, 143),
        ({"type": "system", "subtype": "init"}, 0),
    ],
)
def test_claude_raises_unless_the_turn_ended_with_a_clean_result(
    tmp_path, result, code
):
    command = _fake_claude(tmp_path, [json.dumps(result)], code)
    with pytest.raises(RuntimeError, match="claude exited"):
        jfc._claude(command, tmp_path, tmp_path / "cfg", tmp_path / "tokens", 0)


def _assistant_of(request, **usage):
    """One block of the model's reply to `request`, with what the request counts."""
    message = {
        "id": request,
        "usage": usage,
        "content": [{"type": "text", "text": "."}],
    }
    return json.dumps({"type": "assistant", "message": message})


def test_claude_counts_each_request_once_and_what_the_stream_shows_of_the_rest(
    tmp_path,
):
    ledger = tmp_path / "tokens"
    ledger.write_text("1000")
    subagent = json.loads(_assistant_of("s", output_tokens=7))
    subagent["parent_tool_use_id"] = "toolu_1"
    compaction = {"type": "system", "subtype": "compact_boundary"}
    full = {"input_tokens": 10, "cache_read_input_tokens": 200, "output_tokens": 5}
    lines = [
        _assistant_of("a", input_tokens=10, cache_read_input_tokens=200),
        _assistant_of("a", **full),
        _assistant_of(
            "b",
            input_tokens=1,
            cache_creation_input_tokens=30,
            cache_creation={"ephemeral_5m_input_tokens": 30},
            output_tokens=None,
        ),
        _assistant_of("a", **full),
        json.dumps(subagent),
        json.dumps({**compaction, "compact_metadata": {"pre_tokens": 400}}),
        json.dumps({"type": "user", "message": {"content": "tool output"}}),
        json.dumps({"type": "result", "is_error": False, "result": "done"}),
    ]

    said = jfc._claude(
        _fake_claude(tmp_path, lines), tmp_path, tmp_path / "c", ledger, 0
    )

    assert said == "done" and ledger.read_text() == str(1000 + 215 + 31 + 7 + 400)


@pytest.mark.parametrize("reported, counted", [(100, 900), (900, 900), (5000, 5000)])
def test_a_turn_counts_by_its_own_totals_when_they_say_more(
    tmp_path, reported, counted
):
    ledger = tmp_path / "tokens"
    ledger.write_text("10")
    models = {
        "opus": {"inputTokens": reported // 2, "cacheReadInputTokens": reported // 4},
        "haiku": {"outputTokens": reported // 4, "costUSD": 1.5},
    }
    lines = [
        _assistant_of("a", input_tokens=900),
        json.dumps({"type": "result", "is_error": True, "modelUsage": models}),
    ]

    with pytest.raises(RuntimeError):
        jfc._claude(
            _fake_claude(tmp_path, lines, 1), tmp_path, tmp_path / "c", ledger, 0
        )

    assert ledger.read_text() == str(10 + counted)


def test_claude_records_a_request_as_it_arrives(tmp_path):
    ledger, seen = tmp_path / "tokens", tmp_path / "seen"
    script = tmp_path / "claude.py"
    script.write_text(
        "import pathlib, time\n"
        f"print({_assistant_of('a', input_tokens=10)!r}, flush=True)\n"
        f"ledger = pathlib.Path({str(ledger)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while time.monotonic() < deadline and not ledger.exists():\n"
        "    time.sleep(0.01)\n"
        f"pathlib.Path({str(seen)!r}).write_text(ledger.read_text())\n"
    )
    command = [sys.executable, str(script)]

    with pytest.raises(RuntimeError):
        jfc._claude(command, tmp_path, tmp_path / "c", ledger, 0)

    assert seen.read_text() == ledger.read_text() == "10"


@pytest.mark.parametrize("held", ["", "12 tokens"])
def test_a_count_that_cannot_be_read_stops_the_workspace(tmp_path, held):
    ledger = tmp_path / "tokens"
    ledger.write_text(held)
    with pytest.raises(jfc.flyte.errors.NonRecoverableError, match=str(ledger)):
        jfc._claude(_fake_claude(tmp_path, []), tmp_path, tmp_path / "c", ledger, 0)


def test_a_subagent_counts_toward_the_limit_and_can_reach_it(tmp_path):
    ledger = tmp_path / "tokens"
    subagent = json.loads(_assistant_of("s", input_tokens=100))
    subagent["parent_tool_use_id"] = "toolu_1"
    lines = [
        json.dumps(subagent),
        json.dumps({"type": "result", "is_error": False, "result": "done"}),
    ]

    said = jfc._claude(
        _fake_claude(tmp_path, lines), tmp_path, tmp_path / "c", ledger, 100
    )

    assert said == jfc.SPENT.format(tokens=100) and ledger.read_text() == "100"


@pytest.mark.parametrize("deaf", [False, True])
def test_claude_ends_a_turn_that_reaches_the_limit(tmp_path, monkeypatch, deaf):
    monkeypatch.setattr(jfc, "GRACE", 0.5 if deaf else 20)
    ledger, closed = tmp_path / "tokens", tmp_path / "closed"
    ledger.write_text("90")
    on_term = (
        "signal.SIG_IGN"
        if deaf
        else f"lambda *_: (open({str(closed)!r}, 'w').close(), sys.exit(0))"
    )
    script = tmp_path / "claude.py"
    script.write_text(
        "import signal, sys, time\n"
        f"signal.signal(signal.SIGTERM, {on_term})\n"
        f"print({_assistant_of('a', input_tokens=4)!r}, flush=True)\n"
        f"print({_assistant_of('b', input_tokens=6)!r}, flush=True)\n"
        "time.sleep(60)\n"
        f"print({_assistant_of('c', input_tokens=1000)!r}, flush=True)\n"
    )
    started = jfc.time.monotonic()
    command = [sys.executable, str(script)]

    said = jfc._claude(command, tmp_path, tmp_path / "c", ledger, 100)

    assert said == jfc.SPENT.format(tokens=100) and ledger.read_text() == "100"
    assert jfc.time.monotonic() - started < 10
    # Told to end first, so that it can close its session; killed only if it does not.
    assert closed.exists() is not deaf


@pytest.mark.parametrize(
    "result",
    [
        {"api_error_status": 429, "result": "API Error: 429"},
        {"result": "You've hit your session limit · resets 3pm (UTC)"},
        {"result": "You've hit your weekly limit · resets Oct 20"},
    ],
)
def test_a_turn_the_usage_limit_stopped_is_told_from_other_failures(tmp_path, result):
    stopped = {"type": "result", "is_error": True, **result}
    command = _fake_claude(tmp_path, [json.dumps(stopped)], 1)
    with pytest.raises(jfc.Limited) as limited:
        jfc._claude(command, tmp_path, tmp_path / "cfg", tmp_path / "tokens", 0)
    assert limited.value.resets is None

    overloaded = {"type": "result", "is_error": True, "api_error_status": 529}
    command = _fake_claude(tmp_path, [json.dumps(overloaded)], 1)
    with pytest.raises(RuntimeError) as failed:
        jfc._claude(command, tmp_path, tmp_path / "cfg", tmp_path / "tokens", 0)
    assert not isinstance(failed.value, jfc.Limited)


def test_a_limited_turn_says_when_the_limit_resets(tmp_path):
    events = [
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}},
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "rejected", "resetsAt": 1760600000},
        },
        {"type": "result", "is_error": True, "result": "API Error"},
    ]
    command = _fake_claude(tmp_path, [json.dumps(e) for e in events], 1)

    with pytest.raises(jfc.Limited) as limited:
        jfc._claude(command, tmp_path, tmp_path / "cfg", tmp_path / "tokens", 0)

    assert limited.value.resets == 1760600000


@pytest.mark.parametrize(
    "answer, limited",
    [
        ({"is_error": True, "api_error_status": 429}, True),
        (
            {
                "is_error": True,
                "result": "You've hit your weekly limit · resets Oct 20",
            },
            True,
        ),
        ({"is_error": False, "result": "ok"}, False),
    ],
)
def test_a_request_outside_the_session_tells_whether_the_limit_holds(
    tmp_path, monkeypatch, answer, limited
):
    (tmp_path / "answer.json").write_text(json.dumps(answer))
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text(
        "#!/bin/sh\n"
        f'[ "$CLAUDE_CONFIG_DIR" = "{tmp_path / "cfg"}" ] || exit 9\n'
        f'cat "{tmp_path / "answer.json"}"\n'
    )
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{claude.parent}{os.pathsep}{os.environ['PATH']}")

    assert jfc._still_limited(tmp_path / "cfg") is limited


def test_a_rejected_credential_is_not_worth_a_retry(tmp_path):
    result = {"type": "result", "is_error": True, "api_error_status": 401}
    command = _fake_claude(tmp_path, [json.dumps(result)], 1)
    with pytest.raises(jfc.flyte.errors.NonRecoverableError):
        jfc._claude(command, tmp_path, tmp_path / "cfg", tmp_path / "tokens", 0)


# --- the task ---


@pytest.fixture
def stretch(root, monkeypatch):
    """`advance` with the setup done, the agent recorded, and no real pauses."""
    work, _ = _workspace(root)
    state = types.SimpleNamespace(
        attempt_number=0,
        action=types.SimpleNamespace(run_name="ws"),
        prompts=[],
        limits=[],
        slept=[],
    )

    def claude(command, cwd, config, ledger, limit):
        state.prompts.append(command[2])
        state.limits.append((ledger, limit))
        (config / "projects/slug").mkdir(parents=True, exist_ok=True)
        (config / "projects/slug" / f"{_session(command)[1]}.jsonl").write_text("{}")
        return "At the human gate."

    monkeypatch.setattr(jfc.flyte, "ctx", lambda: state, raising=False)
    monkeypatch.setattr(jfc, "_prepare", lambda home, analysis: work)
    monkeypatch.setattr(jfc, "_claude", claude)
    monkeypatch.setattr(jfc.time, "sleep", state.slept.append)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stand-in")
    monkeypatch.setenv(jfc.GATEWAY_TOKEN, "stand-in")
    return state


def test_advance_refuses_to_start_without_credentials(root, monkeypatch):
    for name in jfc.CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(jfc, "_prepare", lambda *args: pytest.fail("set up anyway"))

    with pytest.raises(jfc.flyte.errors.NonRecoverableError):
        jfc.advance(ANALYSIS, "ws", "")


def test_advance_refuses_to_start_without_a_gateway_token(root, stretch, monkeypatch):
    monkeypatch.delenv(jfc.GATEWAY_TOKEN)

    with pytest.raises(jfc.flyte.errors.NonRecoverableError, match=jfc.GATEWAY_TOKEN):
        jfc.advance(ANALYSIS, "ws", "")
    assert stretch.prompts == []


def test_advance_returns_and_keeps_what_the_agent_said(root, stretch):
    assert jfc.advance(ANALYSIS, "ws", "") == "At the human gate."
    assert (root / "ws/said.md").read_text() == "At the human gate."
    assert stretch.slept == []


def test_each_turn_starts_with_the_runners_facility_rules_in_place(
    root, stretch, monkeypatch
):
    memory = root / "ws" / "claude" / "CLAUDE.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("the rules of an older runner")
    read = []

    def claude(command, cwd, config, *counted):
        read.append((config / "CLAUDE.md").read_text())
        return ""

    monkeypatch.setattr(jfc, "_claude", claude)
    jfc.advance(ANALYSIS, "ws", "")

    assert read == [
        jfc.FACILITY.format(
            cores=jfc.CORES, hours=jfc.COMMAND_HOURS, shared=root / "ws" / "shared"
        )
    ]


def test_advance_counts_in_the_workspace_and_refuses_once_its_tokens_are_spent(
    root, stretch
):
    capped = jfc.Analysis(**{**vars(ANALYSIS), "tokens": 500})

    jfc.advance(capped, "ws", "")
    (root / "ws" / "tokens").write_text("500")
    with pytest.raises(jfc.flyte.errors.NonRecoverableError, match="500 tokens"):
        jfc.advance(capped, "ws", "APPROVE")
    jfc.advance(ANALYSIS, "ws", "")

    assert stretch.limits == [
        (root / "ws" / "tokens", 500),
        (root / "ws" / "tokens", 0),
    ]
    assert not (root / "ws" / "replies").exists()


def test_advance_hands_a_reply_to_the_agent_once(root, stretch):
    jfc.advance(ANALYSIS, "ws", "")
    stretch.action.run_name = "run-2"

    jfc.advance(ANALYSIS, "ws", "REGRESS(2)")
    jfc.advance(ANALYSIS, "ws", "REGRESS(2)")  # run-2 retried

    assert stretch.prompts[1:] == [
        jfc.REPLY.format(reply="REGRESS(2)"),
        jfc.INTERRUPTED,
    ]
    assert (root / "ws/replies/run-2").read_text() == "REGRESS(2)"


@pytest.mark.parametrize("reply", ["", "APPROVE"])
def test_advance_refuses_a_workspace_that_is_neither_its_own_nor_there(
    root, stretch, reply
):
    with pytest.raises(jfc.flyte.errors.NonRecoverableError):
        jfc.advance(ANALYSIS, "typo", reply)
    assert stretch.prompts == [] and not (root / "typo").exists()


def test_advance_runs_the_agent_under_the_workspace_lock(root, stretch, monkeypatch):
    def claude(command, cwd, config, *counted):
        with open(root / "ws/lock") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return ""

    monkeypatch.setattr(jfc, "_claude", claude)
    jfc.advance(ANALYSIS, "ws", "")


def _failing(error):
    def claude(*turn):
        raise error

    return claude


def test_advance_waits_after_a_failed_turn_longer_each_time(stretch, monkeypatch):
    monkeypatch.setattr(jfc, "_claude", _failing(RuntimeError("usage limit")))

    for stretch.attempt_number in (0, 1, 4, jfc.RETRIES):
        with pytest.raises(RuntimeError, match="usage limit"):
            jfc.advance(ANALYSIS, "ws", "")

    assert stretch.slept == [15 * 60, 30 * 60, 240 * 60]


@pytest.fixture
def limited(root, stretch, monkeypatch):
    """`advance` on a session that exists, with each turn's outcome taken in turn
    from `outcomes`, and the clock at 1000."""
    state = types.SimpleNamespace(outcomes=[], prompts=[], probes=[])

    def claude(command, cwd, config, ledger, limit):
        state.prompts.append(command[2])
        outcome = state.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(jfc, "_claude", claude)
    monkeypatch.setattr(jfc, "_still_limited", lambda config: state.probes.pop(0))
    monkeypatch.setattr(jfc.time, "time", lambda: 1000.0)
    _, session = _session(jfc._turn(root / "jfc/analyses/zz", root / "ws/claude", ""))
    (root / "ws/claude/projects/slug").mkdir(parents=True)
    (root / "ws/claude/projects/slug" / f"{session}.jsonl").write_text("{}")
    stretch.action.run_name = "run-2"
    return state


def test_advance_sleeps_until_the_limit_resets_then_resumes_once(stretch, limited):
    limited.outcomes = [jfc.Limited("limit", resets=4600.0), "At the human gate."]

    assert jfc.advance(ANALYSIS, "ws", "APPROVE") == "At the human gate."

    assert stretch.slept == [3600.0 + 60]
    assert limited.prompts == [jfc.REPLY.format(reply="APPROVE"), jfc.LIFTED]


def test_advance_asks_outside_the_session_while_no_reset_is_known(stretch, limited):
    limited.outcomes = [jfc.Limited("limit"), "At the human gate."]
    limited.probes = [True, True, False]

    assert jfc.advance(ANALYSIS, "ws", "") == "At the human gate."

    assert stretch.slept == [jfc.LIMITED * 60] * 3
    assert limited.prompts == [jfc.INTERRUPTED, jfc.LIFTED]


def test_advance_does_not_wait_on_a_failure_no_retry_follows(stretch, monkeypatch):
    final = jfc.flyte.errors.NonRecoverableError("claude: Invalid API key")
    monkeypatch.setattr(jfc, "_claude", _failing(final))

    with pytest.raises(jfc.flyte.errors.NonRecoverableError):
        jfc.advance(ANALYSIS, "ws", "")

    assert stretch.slept == []
