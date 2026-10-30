"""workflows/agentic-analysis against a stub `flyte` (the SDK is not a test dependency), its
wiring to apps/agentic-analysis, and the analyses beside it. Git, pixi, the download and the
agent are replaced per test."""

import base64
import fcntl
import http.server
import io
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import types
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from common import REPO, load_script

APP = REPO / "apps" / "agentic-analysis"
WORKFLOW = REPO / "workflows" / "agentic-analysis"
EXPERIMENTAL = REPO / "flux" / "experimental" / "kustomization.yaml"


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
    # The launcher and the task find af_profiling beside agentic_analysis.py.
    sys.path.insert(0, str(WORKFLOW))
    try:
        return load_script(
            WORKFLOW / "agentic_analysis.py", "agentic_analysis_flows"
        ), stubs
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


runner, STUBS = _load()
profiling = runner.af_profiling
clusters = runner.af_dask
gpus = runner.af_ray
llm = load_script(APP / "llm_proxy.py", "llm_proxy_for_the_runner")
# What the proxy's /config serves.
ROLES = llm.Proxy(llm.load(APP / "models.toml"), "key", "token").config_view()

ANALYSIS = runner.Analysis(
    name="zz",
    kind="search",
    repo="https://example.org/zz.git",
    ref="c0ffee",
    files=["docs", "ntuplize.py"],
    data="https://example.org/zz.tgz",
)


@pytest.fixture(autouse=True)
def user(tmp_path, monkeypatch):
    """A $HOME of the test's own, which the runner moves OpenCode's configuration out of."""
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    return tmp_path / "user"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    return tmp_path


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _variables(container):
    """A container's environment: each variable's value, or where it comes from."""
    return {v["name"]: v.get("value", v.get("valueFrom")) for v in container["env"]}


# --- wiring ---


def test_flux_lists_every_manifest_of_the_app():
    """Each YAML file of the app is a manifest Flux applies, or its workload
    account's declaration in the af-accounts ConfigMap."""
    kustomization = yaml.safe_load(EXPERIMENTAL.read_text())
    generators = {g["name"]: g for g in kustomization["configMapGenerator"]}
    declarations = {
        line.partition("=")[2] for line in generators["af-accounts"]["files"]
    }
    files = {f"../../apps/agentic-analysis/{path.name}" for path in APP.glob("*.yaml")}
    assert files & declarations == {"../../apps/agentic-analysis/account.yaml"}
    resources = kustomization["resources"]
    assert {r for r in resources if "/apps/agentic-analysis/" in r} == (
        files - declarations
    )


def test_the_task_image_is_built_published_and_pulled_under_one_name():
    inputs = (REPO / ".github/workflows/image-inputs.sh").read_text()
    assert "\tagentic-analysis)" in inputs and "docker/agentic-analysis" in inputs
    ci = yaml.safe_load((REPO / ".github/workflows/ci-images.yml").read_text())
    matrix = {
        m["name"]: m
        for m in ci["jobs"]["build-aux-images"]["strategy"]["matrix"]["include"]
    }
    assert (
        matrix["agentic-analysis"]["dockerfile"] == "docker/agentic-analysis/Dockerfile"
    )
    resolve = ci["jobs"]["resolve"]["steps"][-1]["run"]
    assert " agentic-analysis " in resolve, (
        "publish only moves :latest for names listed in resolve"
    )
    status = (REPO / ".github/workflows/component-status.py").read_text()
    assert '"agentic-analysis",' in status
    assert (REPO / "docker/agentic-analysis/uv.lock").is_file()
    (job,) = docs(APP / "workspace.yaml")
    (container,) = job["spec"]["template"]["spec"]["containers"]
    assert runner.IMAGE == container["image"]
    assert runner.IMAGE.endswith("/purdueaf/agentic-analysis:latest")


def test_the_task_runs_in_the_pod_template_and_project_named_after_the_workflow():
    (template,) = docs(APP / "podtemplate.yaml")
    config = yaml.safe_load((WORKFLOW / "config.yaml").read_text())
    flyte_values = yaml.safe_load((REPO / "apps/flyte/values.yaml").read_text())
    assert runner.env.options["pod_template"] == template["metadata"]["name"]
    assert config["task"]["project"] == runner.NAME
    assert f"{flyte_values['fullnameOverride']}-http." in config["admin"]["endpoint"]


def test_task_pods_work_on_their_accounts_volume_as_its_clusters_do():
    """The account's Dask and Ray pods mount its volume at /<name> as its UID:
    apps/dask-gateway/README.md#workload-accounts."""
    (template,) = docs(APP / "podtemplate.yaml")
    (job,) = docs(APP / "workspace.yaml")
    (account,) = docs(APP / "account.yaml")
    pod = template["template"]["spec"]
    mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
    claims = {
        v["name"]: v["persistentVolumeClaim"]["claimName"]
        for v in pod["volumes"]
        if "persistentVolumeClaim" in v
    }
    (volume,) = [name for name, claim in claims.items() if claim == account["name"]]
    assert mounts[volume]["mountPath"] == str(runner.ROOT) == f"/{account['name']}"
    assert "subPath" not in mounts[volume]
    assert set(claims.values()) == {account["name"], "cvmfs"}
    ids = (pod["securityContext"]["runAsUser"], pod["securityContext"]["runAsGroup"])
    assert ids == (account["uid"], account["gid"])
    assert template["template"]["metadata"]["labels"] == {
        "app.kubernetes.io/part-of": account["name"]
    }

    (container,) = job["spec"]["template"]["spec"]["containers"]
    (made,) = container["volumeMounts"]
    (handed,) = job["spec"]["template"]["spec"]["volumes"]
    assert handed["persistentVolumeClaim"]["claimName"] == account["name"]
    assert container["command"][-1] == "chown {}:{} {}".format(*ids, made["mountPath"])


def test_task_pods_hold_their_accounts_token_where_the_helpers_read_it():
    (template,) = docs(APP / "podtemplate.yaml")
    (secret,) = docs(APP / "secret-account.yaml")
    (account,) = docs(APP / "account.yaml")
    pod = template["template"]["spec"]
    (container,) = pod["containers"]
    env = _variables(container)
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    assert secret["metadata"]["name"] == f"{account['name']}-account"
    (name,) = [
        v["name"]
        for v in pod["volumes"]
        if v.get("secret", {}).get("secretName") == secret["metadata"]["name"]
    ]
    assert "token" in secret["stringData"]
    assert env[clusters.TOKEN_FILE] == f"{mounts[name]['mountPath']}/token"
    assert mounts[name]["readOnly"]
    # Readable by the task user, whom the Secret's files do not belong to.
    assert (
        next(v for v in pod["volumes"] if v["name"] == name)["secret"]["defaultMode"]
        & 0o004
    )


def test_task_pods_reach_the_llm_proxy_with_the_token_it_takes():
    (template,) = docs(APP / "podtemplate.yaml")
    deployment, service = docs(APP / "llm-proxy.yaml")
    (secret,) = docs(APP / "secret-llm-proxy.yaml")
    env = _variables(template["template"]["spec"]["containers"][0])
    (port,) = service["spec"]["ports"]
    (proxy,) = deployment["spec"]["template"]["spec"]["containers"]
    assert env[runner.PROXY] == f"http://{service['metadata']['name']}:{port['port']}"
    taken = _variables(proxy)["LLM_PROXY_TOKEN"]["secretKeyRef"]
    assert env[runner.TOKEN] == {"secretKeyRef": taken}
    assert taken["name"] == secret["metadata"]["name"]
    assert taken["key"] in secret["stringData"]


def test_the_agents_run_on_roles_the_proxy_serves():
    wanted = {"orchestrator", "compaction", *(r for r, _ in runner.AGENTS.values())}
    assert wanted <= ROLES.keys()


def test_opencode_waits_out_the_proxys_keepalives_and_holds():
    """The proxy keeps a held request alive with comments, then lets it go."""
    settings = llm.load(APP / "models.toml").settings
    assert runner.WAITS["chunkTimeout"] > settings.keepalive_seconds * 1000
    assert runner.WAITS["timeout"] > settings.hold_hours * 3_600_000


def test_the_plugin_ships_with_the_task_and_needs_no_package():
    """OpenCode installs no npm package into its read-only configuration."""
    assert runner.env.options["include"] == (runner.PLUGIN.name,)
    assert runner.PLUGIN.parent == WORKFLOW
    imported = re.findall(
        r'^import .* from "([^"]+)";$', runner.PLUGIN.read_text(), re.M
    )
    assert imported and all(name.startswith("node:") for name in imported)


def test_the_workspace_job_and_the_browser_open_the_volume_the_app_declares():
    (volume,) = docs(APP / "volume.yaml")
    (job,) = docs(APP / "workspace.yaml")
    name = volume["metadata"]["name"]
    assert volume["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == (
        "disabled"
    )
    assert volume["spec"]["accessModes"] == ["ReadWriteMany"]
    claims = {
        v["name"]: v["persistentVolumeClaim"]["claimName"]
        for v in job["spec"]["template"]["spec"]["volumes"]
    }
    assert claims["volume"] == name
    browse = (WORKFLOW / "browse.sh").read_text()
    assert f"image={runner.IMAGE}\n" in browse
    assert f'"claimName": "{name}", "readOnly": true' in browse
    assert '"mountPath": "$mount", "readOnly": true' in browse


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
    env = _variables(container)
    hub = yaml.safe_load((REPO / "apps/jupyterhub/jupyterhub/values.yaml").read_text())
    assert env["PYROSCOPE_SERVER"] == _found(hub, "PYROSCOPE_SERVER")
    assert env["PYROSCOPE_APP"] == runner.NAME


def test_task_pods_reach_the_sessions_gateway_from_a_pod_as_their_account():
    """The gateway takes an account's token only from a pod, at its API Service, and
    serves dashboards only through its Traefik; the pods reach schedulers through that
    Traefik's in-cluster Service, at the sessions' port."""
    (template,) = docs(APP / "podtemplate.yaml")
    container = template["template"]["spec"]["containers"][0]
    env = _variables(container)
    spawner = (
        REPO / "apps/jupyterhub/jupyterhub/extraFiles/custom-spawner.py"
    ).read_text()
    (release,) = docs(REPO / "apps/dask-gateway/helmrelease.yaml")
    (ingress,) = docs(REPO / "apps/dask-gateway/ingress.yaml")
    names = sorted(name for name in env if name.startswith("DASK_GATEWAY__"))
    assert names == [
        "DASK_GATEWAY__ADDRESS",
        "DASK_GATEWAY__AUTH__TYPE",
        "DASK_GATEWAY__PROXY_ADDRESS",
        "DASK_GATEWAY__PUBLIC_ADDRESS",
    ]
    assert (
        env["DASK_GATEWAY__ADDRESS"] == f"http://api-{release['metadata']['name']}:8000"
    )
    (rule,) = ingress["spec"]["rules"]
    (path,) = rule["http"]["paths"]
    assert path["path"] == "/"
    assert (
        path["backend"]["service"]["name"] == f"traefik-{release['metadata']['name']}"
    )
    assert env["DASK_GATEWAY__PUBLIC_ADDRESS"] == f"http://{rule['host']}"
    assert env["DASK_GATEWAY__AUTH__TYPE"] == (
        f"{clusters.__name__}.{clusters.AccountAuth.__name__}"
    )
    (port,) = re.findall(r'"DASK_GATEWAY__PROXY_ADDRESS",\s*"[^"]*:(\d+)"', spawner)
    assert env["DASK_GATEWAY__PROXY_ADDRESS"] == (
        f"traefik-{release['metadata']['name']}:{port}"
    )


def test_the_workload_reaches_in_the_cluster_only_what_its_task_pods_are_pointed_at():
    """The egress policy holds the task pods and the account's clusters to each other,
    the pods behind the addresses the task pods are given, and the internet outside
    the cluster's pod network."""
    (policy,) = docs(APP / "networkpolicy.yaml")
    (template,) = docs(APP / "podtemplate.yaml")
    labels = template["template"]["metadata"]["labels"]
    assert policy["spec"]["podSelector"] == {"matchLabels": labels}
    assert policy["spec"]["policyTypes"] == ["Egress"]
    admitted = {}
    blocks = []
    anywhere = set()
    for rule in policy["spec"]["egress"]:
        assert all(set(port) == {"protocol", "port"} for port in rule.get("ports", []))
        ports = {(port["protocol"], port["port"]) for port in rule.get("ports", [])}
        if "to" not in rule:
            anywhere |= ports
        for peer in rule.get("to", []):
            # A pod peer in this namespace only, or an address block.
            assert set(peer) in ({"podSelector"}, {"ipBlock"})
            if "podSelector" in peer:
                assert set(peer["podSelector"]) == {"matchLabels"}
                selected = frozenset(peer["podSelector"]["matchLabels"].items())
                assert selected not in admitted
                admitted[selected] = ports
            else:
                blocks.append((peer["ipBlock"], ports))
    env = _variables(template["template"]["spec"]["containers"][0])

    def deployment(path):
        (found,) = [d for d in docs(path) if d["kind"] == "Deployment"]
        pod = found["spec"]["template"]
        ports = {
            p["containerPort"]
            for c in pod["spec"]["containers"]
            for p in c.get("ports", [])
        }
        return frozenset(found["spec"]["selector"]["matchLabels"].items()), ports

    def released(path, component):
        (release,) = docs(path)
        instance = ("app.kubernetes.io/instance", release["metadata"]["name"])
        return frozenset({instance, *component.items()})

    gateway = REPO / "apps/dask-gateway/helmrelease.yaml"
    flyte = REPO / "apps/flyte/values.yaml"
    pointed = {
        frozenset(labels.items()): set(),
        deployment(APP / "llm-proxy.yaml")[0]: {
            ("TCP", urlsplit(env[runner.PROXY]).port)
        },
        released(gateway, {"app.kubernetes.io/component": "gateway"}): {
            ("TCP", urlsplit(env["DASK_GATEWAY__ADDRESS"]).port)
        },
        released(gateway, {"app.kubernetes.io/component": "traefik"}): {
            ("TCP", int(env["DASK_GATEWAY__PROXY_ADDRESS"].rpartition(":")[2]))
        },
        deployment(REPO / "apps/ray-train/deployment.yaml")[0]: {
            ("TCP", urlsplit(gpus.ADDRESS).port)
        },
        released(REPO / "apps/monitoring/pyroscope/helmrelease.yaml", {}): {
            ("TCP", urlsplit(env["PYROSCOPE_SERVER"]).port)
        },
        deployment(REPO / "apps/flyte/minio.yaml")[0]: {
            (
                "TCP",
                urlsplit(
                    _found(yaml.safe_load(flyte.read_text()), "FLYTE_AWS_ENDPOINT")
                ).port,
            )
        },
    }
    assert admitted == pointed
    for path in (
        APP / "llm-proxy.yaml",
        REPO / "apps/ray-train/deployment.yaml",
        REPO / "apps/flyte/minio.yaml",
    ):
        selected, ports = deployment(path)
        assert {port for _, port in admitted[selected]} <= ports
    # The cluster's resolver, wherever it runs.
    assert anywhere == {("UDP", 53), ("TCP", 53)}
    values = (REPO / "apps/dask-gateway/values.yaml").read_text()
    (pods,) = re.findall(r'POD_NETWORK = ipaddress.ip_network\("([^"]+)"\)', values)
    ((block, ports),) = blocks
    assert block["cidr"] == "0.0.0.0/0" and ports == set()
    assert any(
        ipaddress.ip_network(pods).subnet_of(ipaddress.ip_network(cidr))
        for cidr in block["except"]
    )


def test_task_pods_hold_the_facilitys_proxy_and_the_certificates_to_use_it():
    (template,) = docs(APP / "podtemplate.yaml")
    pod = template["template"]["spec"]
    (container,) = pod["containers"]
    env = _variables(container)
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    volumes = {v["name"]: v for v in pod["volumes"]}
    flux = yaml.safe_load((EXPERIMENTAL.parent / "flux-kustomization.yaml").read_text())
    names = flux["spec"]["postBuild"]["substitute"]

    (proxy,) = [
        n
        for n, v in volumes.items()
        if v.get("secret", {}).get("secretName") == "${x509_secret_name}"
    ]
    secret = volumes[proxy]["secret"]
    assert names["x509_secret_name"]
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
    described = sorted((WORKFLOW / "analyses").glob("*/analysis.toml"))
    assert described
    for path in described:
        runner.launch(str(path.parent))
        run = Launcher.runs[-1]
        analysis = run["analysis"]
        assert path.parent.name == analysis.name
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

    runner.launch(launched)

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
        runner.launch(launched)
    assert Launcher.runs == []


def test_launch_starts_a_run_named_after_the_analysis_in_a_workspace_of_its_own(
    launched,
):
    runner.launch(launched)
    runner.launch(launched)

    first, _ = Launcher.runs
    assert first["task"] is runner.advance
    assert first["analysis"] == runner.Analysis(
        "zz", "search", "https://example.org/zz.git", "c0ffee"
    )
    assert first["name"].startswith("zz-") and first["workspace"] == first["name"]
    assert first["reply"] == ""
    assert Launcher.projects == [runner.NAME]


def test_launch_carries_a_reply_whole_to_the_workspace_it_names(launched):
    runner.launch(launched, "zz-1013-120000", "ITERATE:", "redo the fit")

    (run,) = Launcher.runs
    assert run["workspace"] == "zz-1013-120000"
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

    monkeypatch.setattr(runner, "_checkout", checkout)
    monkeypatch.setattr(runner, "_run", run)
    monkeypatch.setattr(runner, "_data", lambda analysis: data)
    return calls


def test_prepare_stages_the_analysis_into_its_scaffold(root, monkeypatch):
    data = root / "data"
    data.mkdir()
    calls = _fake_tools(monkeypatch, data)

    work = runner._prepare(root / "run", ANALYSIS)

    assert work == root / "run/jfc/analyses/zz"
    assert ("checkout", "jfc", *runner.JFC) in calls
    assert ("checkout", "source", ANALYSIS.repo, ANALYSIS.ref) in calls
    assert ("src/scaffold_analysis.py", "analyses/zz", "--type", "search") in calls
    assert (work / "data").resolve() == data.resolve()
    assert (work / "prompt.md").read_text() == "search for ZZ"
    assert (work / "docs/paper.pdf").is_file()
    assert (work / "ntuplize.py").is_file()
    assert calls[-2:] == [
        ("pixi", "add", "--no-install", *runner.CONDA),
        ("pixi", "add", "--pypi", *runner.PACKAGES),
    ]


def test_prepare_stages_an_analysis_that_brings_its_own_files(root, monkeypatch):
    calls = _fake_tools(monkeypatch, root)
    local = runner.Analysis(
        "zz",
        "search",
        files=["datasets.yaml", "notes"],
        sources={
            "prompt.md": "search for ZZ",
            "datasets.yaml": "2018: []",
            "notes/trigger.md": "IsoMu24",
        },
    )

    work = runner._prepare(root / "run", local)

    assert [call for call in calls if call[:2] == ("checkout", "source")] == []
    assert (work / "prompt.md").read_text() == "search for ZZ"
    assert (work / "datasets.yaml").read_text() == "2018: []"
    assert (work / "notes/trigger.md").read_text() == "IsoMu24"
    assert not (work / "data").exists()


def test_the_helpers_start_the_profiler_in_every_python_of_the_environment(tmp_path):
    packages, seen = tmp_path / "site-packages", tmp_path / "seen.json"
    packages.mkdir()
    (packages / "pyroscope.py").write_text(
        "import json, os\n"
        "def configure(**options):\n"
        "    with open(os.environ['SEEN'], 'w') as seen:\n"
        "        json.dump(options, seen)\n"
        "def shutdown():\n"
        "    pass\n"
    )

    for helper in runner._helpers(tmp_path).iterdir():
        shutil.copy(helper, packages)
    for module in (profiling, clusters, gpus):
        installed = packages / f"{module.__name__}.py"
        assert installed.read_text() == open(module.__file__).read()
    env = {
        **os.environ,
        "PYROSCOPE_SERVER": "http://pyroscope:4040",
        "PYROSCOPE_APP": "agentic-analysis",
        "AF_ANALYSIS": "zz",
        "AF_WORKSPACE": "ws",
        "SEEN": str(seen),
    }
    load = f"import site; site.addsitedir({str(packages)!r})"
    subprocess.run([sys.executable, "-c", load], env=env, check=True)

    options = json.loads(seen.read_text())
    assert options["application_name"] == "agentic-analysis"
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


def test_prepare_puts_the_scope_of_an_analysis_after_its_prompt(root, monkeypatch):
    _fake_tools(monkeypatch, root)
    narrowed = runner.Analysis(**{**vars(ANALYSIS), "scope": "Two samples only."})

    work = runner._prepare(root / "run", narrowed)
    runner._prepare(root / "run", narrowed)

    assert (work / "prompt.md").read_text() == "search for ZZ\n\nTwo samples only."


def test_prepare_stages_only_the_prompt_of_an_analysis_that_names_nothing_else(
    root, monkeypatch
):
    _fake_tools(monkeypatch, root)
    bare = runner.Analysis("zz", "search", ANALYSIS.repo, ANALYSIS.ref)

    work = runner._prepare(root / "run", bare)

    assert sorted(path.name for path in work.iterdir()) == ["prompt.md"]


def test_prepare_leaves_a_staged_workspace_to_the_agent(root, monkeypatch):
    calls = _fake_tools(monkeypatch, root)
    work = runner._prepare(root / "run", ANALYSIS)
    (work / "prompt.md").unlink()
    (work / "experiment_log.md").write_text("the agent's work")
    calls.clear()

    assert runner._prepare(root / "run", ANALYSIS) == work

    assert calls == []
    assert (work / "experiment_log.md").is_file()


def test_prepare_redoes_a_setup_that_died_before_its_last_step(root, monkeypatch):
    calls = _fake_tools(monkeypatch, root)
    run = runner._run

    def dies_installing(*command, cwd):
        run(*command, cwd=cwd)
        if command[0] == "pixi":
            raise OSError("killed")

    monkeypatch.setattr(runner, "_run", dies_installing)
    with pytest.raises(OSError):
        runner._prepare(root / "run", ANALYSIS)
    monkeypatch.setattr(runner, "_run", run)
    calls.clear()

    runner._prepare(root / "run", ANALYSIS)

    assert [call[:2] for call in calls[:2]] == [
        ("checkout", "jfc"),
        ("checkout", "source"),
    ]
    assert calls[-2:] == [
        ("pixi", "add", "--no-install", *runner.CONDA),
        ("pixi", "add", "--pypi", *runner.PACKAGES),
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

    monkeypatch.setattr(runner.urllib.request, "urlretrieve", urlretrieve)

    data = runner._data(ANALYSIS)
    assert runner._data(ANALYSIS) == data and data.parent == root / "data"
    assert (data / "ZZTo4L.root").read_bytes() == b"events"
    assert fetched == [ANALYSIS.data]

    moved = runner.Analysis(
        "zz", "search", ANALYSIS.repo, ANALYSIS.ref, data="https://example.org/v2.tgz"
    )
    assert runner._data(moved) != data
    assert sorted(path.name for path in (root / "data").iterdir()) == sorted(
        [data.name, runner._data(moved).name]
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
        def __init__(self, auth=None):
            state.auth = auth

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
            with open(prefix / "af_dask.lock") as other:
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
    monkeypatch.setenv("AF_ANALYSIS", "zz")
    monkeypatch.setenv("AF_WORKSPACE", "ws")
    monkeypatch.setenv(clusters.TOKEN_FILE, "/etc/af-account/token")
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
                "AF_ANALYSIS": "zz",
                "AF_WORKSPACE": "ws",
                "X509_CERT_DIR": "/cvmfs/certificates",
            },
            "shutdown_on_close": False,
        }
    ]
    assert gateway.adapted == ("new", {"minimum": 0, "maximum": 12})
    assert gateway.client.plugins == []


def test_the_gateway_gets_the_accounts_token_read_again_for_every_request(
    gateway, tmp_path, monkeypatch
):
    token = tmp_path / "token"
    token.write_text("one\n")
    monkeypatch.setenv(clusters.TOKEN_FILE, str(token))

    clusters.client()
    token.write_text("two")

    assert isinstance(gateway.auth, clusters.AccountAuth)
    assert gateway.auth.pre_request(None) == ({"Authorization": "Bearer two"}, None)


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


@pytest.mark.parametrize("ended", ["closing", "closed"])
def test_workers_get_no_proxy_from_a_client_that_ended_while_it_waited(
    gateway, tmp_path, monkeypatch, ended
):
    proxy = tmp_path / "x509up"
    proxy.write_bytes(b"proxy")
    monkeypatch.setenv("X509_USER_PROXY", str(proxy))
    statuses = iter([ended])
    monkeypatch.setattr(
        clusters.time,
        "sleep",
        lambda seconds: setattr(gateway.client, "status", next(statuses)),
    )
    monkeypatch.setattr(
        clusters.threading,
        "Thread",
        lambda target, daemon: types.SimpleNamespace(start=target),
    )

    dask = clusters.client()

    assert len(dask.plugins) == 1


def test_a_renewal_skips_a_reconnecting_client_and_tells_a_running_ones_failure(
    gateway, tmp_path, monkeypatch, capsys
):
    proxy = tmp_path / "x509up"
    proxy.write_bytes(b"proxy")
    monkeypatch.setenv("X509_USER_PROXY", str(proxy))
    statuses = iter(["connecting", "running", "closed"])

    def register_plugin(plugin, name):
        if gateway.client.status != "running":
            raise AttributeError(
                "'NoneType' object has no attribute 'register_worker_plugin'"
            )
        raise OSError("the scheduler is unreachable")

    def sleep(seconds):
        gateway.client.status = next(statuses)
        gateway.client.register_plugin = register_plugin

    monkeypatch.setattr(clusters.time, "sleep", sleep)
    monkeypatch.setattr(
        clusters.threading,
        "Thread",
        lambda target, daemon: types.SimpleNamespace(start=target),
    )

    clusters.client()

    told = capsys.readouterr().err.splitlines()
    assert len(told) == 1 and told[0].endswith("the scheduler is unreachable")


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
    gateway.running = [("theirs", "/agentic-analysis/other")]

    with pytest.raises(RuntimeError, match="theirs is running"):
        clusters.client()

    assert gateway.started == []


# --- the analysis's Ray jobs ---


@pytest.fixture
def ray(monkeypatch, tmp_path):
    """A stand-in for ray.job_submission, recording the clients made."""
    made = []

    def JobSubmissionClient(address, headers):
        made.append({"address": address, "headers": headers, "env": dict(os.environ)})
        return made[-1]

    module = types.ModuleType("ray.job_submission")
    module.JobSubmissionClient = JobSubmissionClient
    monkeypatch.setitem(sys.modules, "ray", types.ModuleType("ray"))
    monkeypatch.setitem(sys.modules, "ray.job_submission", module)
    (tmp_path / "token").write_text("account token\n")
    monkeypatch.setenv(clusters.TOKEN_FILE, str(tmp_path / "token"))
    monkeypatch.setenv("JUPYTERHUB_API_TOKEN", "hub token")
    for name in ("RAY_AUTH_MODE", "RAY_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return made


def test_ray_jobs_run_in_the_environment_on_the_settings_asked_for(ray):
    gpus.client(max_workers=4, gpus_per_worker=2)

    (made,) = ray
    assert made["address"] == gpus.ADDRESS
    assert made["headers"] == {
        "Authorization": "Bearer account token",
        "af-env": os.path.realpath(sys.prefix),
        "af-max-workers": "4",
        "af-gpus-per-worker": "2",
    }


def test_ray_jobs_may_run_in_another_environment(ray):
    gpus.client(env="/agentic-analysis/ws/env")

    assert ray[0]["headers"]["af-env"] == "/agentic-analysis/ws/env"


def test_the_ray_gateway_takes_the_settings_the_helper_sends():
    gateway = (REPO / "apps/ray-train/gateway.py").read_text()
    prefix = re.search(r'^SETTING_PREFIX = "(.+)"$', gateway, re.M).group(1)
    settings = set(re.findall(r'SETTING_PREFIX \+ "([a-z-]+)"', gateway))
    assert prefix == "af-"
    assert {"env", "max-workers", "gpus-per-worker"} <= settings


# --- the agent's configuration ---


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setenv(runner.PROXY, "http://llm-proxy:8080")
    monkeypatch.setenv(runner.TOKEN, "client token")
    return "http://llm-proxy:8080"


def test_opencode_is_configured_from_the_proxys_roles(proxy):
    config = runner._config(ROLES, Path("/local/AGENTS.md"), Path("/ws/zz"))

    af = config["provider"]["af"]
    assert af["npm"] == "@ai-sdk/openai-compatible"
    assert af["options"] == {
        "baseURL": f"{proxy}/v1",
        "apiKey": f"{{env:{runner.TOKEN}}}",
        **runner.WAITS,
    }
    assert af["models"].keys() == ROLES.keys()
    for name, role in ROLES.items():
        model = af["models"][name]
        # OpenCode compacts at context - output, and at most OUTPUT_MAX below it.
        assert model["limit"]["context"] == role["context"]
        assert model["limit"]["output"] == role["context"] - role["compact_at"]
        assert 0 <= model["limit"]["output"] <= runner.OUTPUT_MAX
        assert ("image" in model["modalities"]["input"]) == (role["images"] > 0)
    agents = config["agent"]
    assert agents["orchestrator"]["mode"] == "primary"
    assert agents["orchestrator"]["model"] == "af/orchestrator"
    spawns = agents["orchestrator"]["permission"]["task"]
    assert list(spawns) == ["*", *runner.AGENTS] and spawns.pop("*") == "deny"
    assert set(spawns.values()) == {"allow"}
    for name, (role, description) in runner.AGENTS.items():
        assert agents[name] == {
            "mode": "subagent",
            "model": f"af/{role}",
            "description": description,
            "permission": {"task": "deny"},
        }
    assert agents["compaction"] == {"model": "af/compaction"}
    for builtin in ("build", "plan", "general", "explore", "title"):
        assert agents[builtin] == {"disable": True}
    assert config["default_agent"] == "orchestrator"
    assert config["enabled_providers"] == ["af"]
    # JFC's CLAUDE.md even beside an AGENTS.md, which OpenCode would load instead.
    assert config["instructions"] == ["/local/AGENTS.md", "/ws/zz/CLAUDE.md"]
    assert (config["snapshot"], config["share"], config["subagent_depth"]) == (
        False,
        "disabled",
        1,
    )
    assert config["compaction"] == {"auto": True, "prune": True}


def test_a_proxy_without_a_role_the_agents_need_stops_the_workspace(proxy):
    fewer = {name: role for name, role in ROLES.items() if name != "bibtex"}
    with pytest.raises(runner.flyte.errors.NonRecoverableError, match="bibtex"):
        runner._config(fewer, Path("/local/AGENTS.md"), Path("/ws/zz"))


@pytest.mark.parametrize("below", [-1, runner.OUTPUT_MAX + 1])
def test_a_role_compacting_where_opencode_cannot_stops_the_workspace(proxy, below):
    role = ROLES["executor"]
    odd = {**ROLES, "executor": {**role, "compact_at": role["context"] - below}}
    with pytest.raises(runner.flyte.errors.NonRecoverableError, match="executor"):
        runner._config(odd, Path("/local/AGENTS.md"), Path("/ws/zz"))


def _decides(rules, permission, path):
    """OpenCode's verdict on a call: the last rule whose permission and
    pattern match, with `*` matching anything, `/` included."""

    def match(text, pattern):
        return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), text, re.S)

    verdict = "ask"
    for key, value in rules.items():
        for pattern, action in (
            value if isinstance(value, dict) else {"*": value}
        ).items():
            if match(permission, key) and match(path, pattern):
                verdict = action
    return verdict


def test_nothing_asks_and_opencodes_configuration_is_not_the_agents_to_write():
    rules = runner.PERMISSION
    assert next(iter(rules.items())) == ("*", "allow")
    # A `task` rule here would let subagents start subagents.
    assert "task" not in rules
    for permission in ("bash", "read", "external_directory", "doom_loop", "webfetch"):
        assert _decides(rules, permission, "/cvmfs/cms.cern.ch/x") == "allow"
    assert _decides(rules, "question", "*") == "deny"
    assert _decides(rules, "edit", "phase1_strategy/outputs/plan.md") == "allow"
    for path in (
        ".opencode/plugin/x.js",
        "phase2/.opencode/agents/a.md",
        "../../.opencode/opencode.json",
        "opencode.json",
        "phase3/opencode.jsonc",
        "AGENTS.md",
        "phase1_strategy/CONTEXT.md",
    ):
        assert _decides(rules, "edit", path) == "deny"


@pytest.fixture
def workspace(tmp_path):
    home = tmp_path / "ws"
    work = home / "jfc" / "analyses" / "zz"
    work.mkdir(parents=True)
    (home / "opencode").mkdir()
    (work / "prompt.md").write_text("search for ZZ")
    return work


def test_setup_writes_opencodes_configuration_read_only_off_the_workspace(
    tmp_path, workspace, proxy, user, monkeypatch
):
    for name in runner.XDG:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", "/scratch/cache")
    home = workspace.parents[2]
    env = runner._setup(tmp_path / "local", ROLES, workspace, home)

    config = Path(env["OPENCODE_CONFIG_DIR"])
    assert config == Path(env["XDG_CONFIG_HOME"]) / "opencode"
    assert config.is_relative_to(tmp_path / "local")
    plugin_file = config / "plugin" / runner.PLUGIN.name
    assert plugin_file.read_text() == runner.PLUGIN.read_text()
    for directory in (config / "plugin", config, config.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o555
    facility, claude = json.loads(env["OPENCODE_CONFIG_CONTENT"])["instructions"]
    # OpenCode's global AGENTS.md, so it loads no ~/.claude/CLAUDE.md.
    assert Path(facility) == config / "AGENTS.md"
    assert Path(claude) == workspace / "CLAUDE.md"
    assert Path(facility).read_text() == runner.FACILITY.format(
        cores=runner.CORES,
        hours=runner.COMMAND_HOURS,
        minutes=runner.COMMAND_MINUTES,
        python=runner.PYTHON,
        root=runner.ROOT,
    )
    for file in (plugin_file, Path(facility)):
        assert stat.S_IMODE(file.stat().st_mode) == 0o444
    assert json.loads(env["OPENCODE_PERMISSION"]) == runner.PERMISSION
    # The sessions stay with the workspace; the rest stays with the pod.
    assert env["XDG_DATA_HOME"] == str(home / "opencode" / "data")
    for name in ("XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert Path(env[name]).is_relative_to(tmp_path / "local")
    assert env["OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS"] == str(
        runner.COMMAND_MINUTES * 60_000
    )
    plugin = json.loads(env["AF_PLUGIN"])
    assert plugin["records"] == str(home / "opencode" / "records.jsonl")
    assert (Path(plugin["helpers"]) / plugin["hook"]).is_file()
    assert plugin["tasks"] == str(home / "opencode" / "tasks")
    assert plugin["seats"] == runner.SEATS
    assert plugin["timeout"] == runner.COMMAND_HOURS * 3_600_000
    assert plugin["images"] == {
        "orchestrator": ROLES["orchestrator"]["images"],
        **{name: ROLES[role]["images"] for name, (role, _) in runner.AGENTS.items()},
    }
    # The agents' commands keep the pod's own, writable, places.
    assert plugin["env"] == {
        "XDG_CONFIG_HOME": str(user / ".config"),
        "XDG_DATA_HOME": str(user / ".local/share"),
        "XDG_STATE_HOME": str(user / ".local/state"),
        "XDG_CACHE_HOME": "/scratch/cache",
    }
    assert (env["AF_ANALYSIS"], env["AF_WORKSPACE"]) == ("zz", "ws")
    # Either would hide the JFC CLAUDE.md files from the agents.
    added = env.keys() - os.environ.keys()
    assert not [name for name in added if name.startswith("OPENCODE_DISABLE_PROJECT")]
    assert not [name for name in added if name.startswith("OPENCODE_DISABLE_CLAUDE")]


def test_the_images_a_session_holds_fit_in_a_request_the_proxy_takes(
    tmp_path, workspace, proxy
):
    """As many images as its role takes, each as large as the agent may read,
    sent in base64 beside a full context's text."""
    env = runner._setup(tmp_path / "local", ROLES, workspace, workspace.parents[2])
    most = json.loads(env["AF_PLUGIN"])["image_bytes"]

    roles = {"orchestrator": "orchestrator"}
    roles |= {name: role for name, (role, _) in runner.AGENTS.items()}
    assert most.keys() == roles.keys()
    for name, role in roles.items():
        limits = ROLES[role]
        if not limits["images"]:
            assert most[name] == 0
            continue
        images = limits["images"] * len(base64.b64encode(bytes(most[name])))
        text = limits["context"] * runner.TOKEN_BYTES
        assert most[name] > 0 and images + text <= limits["max_body_bytes"]


def _retried(raised):
    """Whether the task retries the failure pytest caught in `raised`."""
    return not isinstance(raised.value, runner.flyte.errors.NonRecoverableError)


def test_roles_are_asked_of_the_proxy_with_the_clients_token(monkeypatch):
    class Proxy(http.server.BaseHTTPRequestHandler):
        failing = 0

        def log_message(self, *args):
            pass

        def do_GET(self):
            allowed = self.headers["Authorization"] == "Bearer client token"
            body = json.dumps(ROLES).encode() if allowed else b"{}"
            ok = 200 if allowed and self.path == "/config" else 401
            self.send_response(Proxy.failing or ok)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(runner.PROXY, f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv(runner.TOKEN, "client token")
    try:
        assert runner._roles() == ROLES
        Proxy.failing = 502
        with pytest.raises(RuntimeError) as failed:
            runner._roles()
        Proxy.failing = 0
        monkeypatch.setenv(runner.TOKEN, "stale token")
        with pytest.raises(runner.flyte.errors.NonRecoverableError):
            runner._roles()
    finally:
        server.shutdown()
        server.server_close()
    with pytest.raises(RuntimeError) as refused:
        runner._roles()
    assert _retried(failed) and _retried(refused)


def test_opencodes_configuration_left_in_the_workspace_is_moved_aside(workspace, user):
    home = workspace.parents[2]
    planted = [
        workspace / ".opencode" / "plugin" / "x.js",
        workspace / "phase2" / "opencode.json",
        workspace / "opencode.jsonc",
        workspace / "AGENTS.md",
        workspace / "phase2" / "CONTEXT.md",
        home / "jfc" / "opencode.json",
        home / "opencode.json",
        user / ".opencode" / "plugin" / "x.js",
    ]
    for path in planted:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    kept = [
        workspace / ".pixi" / "envs" / "opencode.json",
        workspace / "phase2" / "fit.py",
        workspace / "phase2" / "CLAUDE.md",
    ]
    for path in kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")

    runner._quarantine(workspace, home)

    assert [path for path in planted if path.exists()] == []
    assert all(path.exists() for path in kept)
    (aside,) = (home / "opencode" / "quarantine").iterdir()
    assert (aside / "jfc/analyses/zz/.opencode/plugin/x.js").is_file()
    assert (aside / "jfc/analyses/zz/phase2/opencode.json").is_file()
    assert (aside / "jfc/analyses/zz/AGENTS.md").is_file()
    assert (aside / "opencode.json").is_file()
    assert (aside / "home/.opencode/plugin/x.js").is_file()


# --- the agent ---


def test_first_turn_opens_a_session_with_the_analysis_prompt(workspace):
    command, prompt = runner._turn(workspace, workspace.parents[2], "")

    assert command == [
        "opencode",
        "run",
        "--format",
        "json",
        "--dir",
        str(workspace),
        "--agent",
        "orchestrator",
    ]
    assert prompt == runner.UNATTENDED + "search for ZZ"


def test_later_turns_resume_that_session_with_the_reply_or_a_nudge(workspace):
    home = workspace.parents[2]
    (home / "opencode" / "session").write_text("ses_1\n")

    replied, told = runner._turn(workspace, home, "APPROVE")
    nudged, nudge = runner._turn(workspace, home, "")

    assert replied == nudged and replied[-2:] == ["--session", "ses_1"]
    assert told == runner.REPLY.format(reply="APPROVE")
    assert nudge == runner.INTERRUPTED


def _fake_opencode(tmp_path, events, code=0, records=({"kind": "loaded"},), after=0.0):
    """A stand-in for `opencode run`: keeps its stdin in `stdin`, writes the
    plugin's `records`, prints `events` (a string as it is), lingers `after`
    seconds and exits with `code`, or dies of signal -`code`."""
    script = tmp_path / "opencode.py"
    script.write_text(
        "import json, os, sys, time\n"
        f"open({str(tmp_path / 'stdin')!r}, 'w').write(sys.stdin.read())\n"
        "path = json.loads(os.environ['AF_PLUGIN'])['records']\n"
        f"for record in {list(records)!r}:\n"
        "    open(path, 'a').write(json.dumps(record) + '\\n')\n"
        f"for event in {list(events)!r}:\n"
        "    print(event if isinstance(event, str) else json.dumps(event), flush=True)\n"
        f"time.sleep({after})\n"
        f"os.kill(os.getpid(), {-code}) if {code} < 0 else sys.exit({code})\n"
    )
    return [sys.executable, str(script)]


def _plugin(home):
    return {
        **os.environ,
        "AF_PLUGIN": json.dumps({"records": str(home / "opencode" / "records.jsonl")}),
    }


def _step(*parts, reason="stop", session="ses_1"):
    """The events of one step of the main session."""
    events = [{"type": "step_start", "sessionID": session, "part": {}}]
    for part in parts:
        kind = "tool_use" if "tool" in part else "text"
        events.append({"type": kind, "sessionID": session, "part": part})
    events.append(
        {"type": "step_finish", "sessionID": session, "part": {"reason": reason}}
    )
    return events


def _run(tmp_path, workspace, events, taken=None, **fake):
    home = workspace.parents[2]
    command = _fake_opencode(tmp_path, events, **fake)
    return runner._opencode(
        command, "the prompt", workspace, _plugin(home), home, taken
    )


def test_a_turn_takes_its_prompt_on_stdin_and_keeps_the_session_it_opened(
    tmp_path, workspace
):
    taken = []
    said = _run(
        tmp_path,
        workspace,
        ["not yet", *_step({"text": "at the gate"}, session="ses_9")],
        taken=lambda: taken.append(time.monotonic()),
    )

    assert said == ([], "at the gate", "stop")
    assert (tmp_path / "stdin").read_text() == "the prompt"
    assert (workspace.parents[2] / "opencode" / "session").read_text() == "ses_9"
    # Once, on the first event of the session, which OpenCode sends once it holds the prompt.
    assert len(taken) == 1


def test_a_prompt_opencode_never_took_is_not_taken(tmp_path, workspace):
    taken = []
    with pytest.raises(RuntimeError):
        _run(
            tmp_path,
            workspace,
            ["Error: opencode failed to start"],
            code=1,
            taken=lambda: taken.append(1),
        )
    assert taken == []


def test_a_turn_returns_its_last_reply_and_narrates_the_main_session(
    tmp_path, workspace, capsys
):
    bash = {
        "tool": "bash",
        "state": {"status": "completed", "input": {"command": "pixi run all"}},
    }
    failed = {
        "tool": "read",
        "state": {"status": "error", "input": {"filePath": "a.pdf"}, "error": "a PDF"},
    }
    events = [
        "a warning that is not JSON",
        *_step(bash, failed, {"text": "fit\nconverged"}, reason="tool-calls"),
        {
            "type": "text",
            "sessionID": "ses_1",
            "part": {"text": "Continue", "synthetic": True},
        },
        *_step({"text": "x" * 999}, {"text": "and more"}),
        # Published again as OpenCode prunes its output.
        {
            "type": "tool_use",
            "sessionID": "ses_1",
            "part": {**bash, "state": {**bash["state"], "time": {"compacted": 1}}},
        },
    ]

    assert _run(tmp_path, workspace, events) == (
        [],
        "x" * 999 + "\n\nand more",
        "stop",
    )
    logged = [line.split("] ", 1)[1] for line in capsys.readouterr().out.splitlines()]
    assert logged == [
        "a warning that is not JSON",
        'bash {"command": "pixi run all"}',
        'read {"filePath": "a.pdf"} failed: a PDF',
        "fit converged",
        "x" * runner.LOG_LINE,
        "and more",
        "turn over: 2 steps, the last ending in stop",
    ]


def test_subagents_are_narrated_from_the_plugins_records(tmp_path, workspace, capsys):
    records = [
        {"kind": "loaded"},
        {
            "kind": "tool",
            "parent": None,
            "agent": "orchestrator",
            "tool": "task",
            "status": "completed",
            "input": "{}",
        },
        {
            "kind": "tool",
            "parent": "ses_1",
            "agent": "executor",
            "tool": "bash",
            "status": "completed",
            "input": '{"command":"ls"}',
        },
        {
            "kind": "error",
            "parent": "ses_1",
            "agent": "arbiter",
            "error": "APIError",
            "message": "gone",
        },
        {"kind": "cut", "parent": "ses_1", "agent": "note_writer"},
        {"kind": "beat", "session": "ses_2"},
    ]
    with open(workspace.parents[2] / "opencode" / "records.jsonl", "w") as earlier:
        earlier.write(json.dumps({**records[2], "agent": "an earlier turn's"}) + "\n")

    _run(tmp_path, workspace, _step({"text": "done"}), records=records)

    logged = [line.split("] ", 1)[1] for line in capsys.readouterr().out.splitlines()]
    assert [line for line in logged if line.startswith("  ")] == [
        '  executor bash {"command":"ls"}',
        "  arbiter APIError: gone",
        "  note_writer: the reply ran out of output tokens",
    ]


@pytest.mark.parametrize(
    "events, code, records",
    [
        (
            _step(reason="error")
            + [
                {
                    "type": "error",
                    "error": {
                        "name": "APIError",
                        "data": {"message": "502", "statusCode": 502},
                    },
                }
            ],
            1,
            ({"kind": "loaded"},),
        ),
        (_step({"text": "done"}), 143, ({"kind": "loaded"},)),
        # The plugin did not load: OpenCode goes on without it, unguarded.
        (_step({"text": "done"}), 0, ()),
    ],
)
def test_a_turn_raises_unless_it_ended_cleanly_with_its_plugin(
    tmp_path, workspace, events, code, records
):
    with pytest.raises(RuntimeError, match="opencode") as raised:
        _run(tmp_path, workspace, events, code=code, records=records)
    assert _retried(raised)


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_is_not_worth_a_retry(tmp_path, workspace, status):
    error = {
        "name": "APIError",
        "data": {"message": "a bearer token is required", "statusCode": status},
    }
    with pytest.raises(runner.flyte.errors.NonRecoverableError, match="bearer"):
        _run(tmp_path, workspace, [{"type": "error", "error": error}], code=1)


OVERFLOW = [
    {
        "type": "error",
        "error": {"name": "ContextOverflowError", "data": {"message": "too long"}},
    },
    # OpenCode's compaction, a step of the session.
    *_step({"text": "SUMMARY of the session"}),
]
# OpenCode's prompt to go on after a compaction.
CONTINUE = {
    "type": "text",
    "sessionID": "ses_1",
    "part": {
        "text": "Continue if you have next steps",
        "synthetic": True,
        "metadata": {"compaction_continue": True},
    },
}


@pytest.mark.parametrize(
    "events, said",
    [
        ([*OVERFLOW, *_step({"text": "done"})], "done"),
        # OpenCode replayed the human's message, so no prompt to go on follows the summary.
        ([*OVERFLOW, *_step()], ""),
        # The replay overflowed too, and the second compaction prompts it to go on.
        ([*OVERFLOW, *OVERFLOW, CONTINUE, *_step({"text": "done"})], "done"),
    ],
)
def test_a_context_overflow_opencode_compacted_away_is_no_failure(
    tmp_path, workspace, events, said
):
    assert _run(tmp_path, workspace, events, code=1) == ([], said, "stop")


@pytest.mark.parametrize(
    "events, code",
    [
        # Killed, as by the OOM killer, after it went on.
        ([*OVERFLOW, *_step({"text": "Running the fit now."})], -9),
        # Crashed in the middle of its work.
        ([*OVERFLOW, *_step({"text": "Running the fit now."}, reason="tool-calls")], 1),
        # Crashed before it went on from the compaction.
        (OVERFLOW, 1),
        # An overflow it never compacted.
        (OVERFLOW[:1], 1),
    ],
)
def test_a_turn_that_dies_after_a_context_overflow_fails(
    tmp_path, workspace, events, code
):
    with pytest.raises(RuntimeError, match="opencode") as raised:
        _run(tmp_path, workspace, events, code=code)
    assert _retried(raised)


# OpenCode's compaction of the session after the step before, and its prompt to go on.
COMPACTED = [*_step({"text": "SUMMARY of the session"}), CONTINUE]


GATE = _step({"text": "At the gate."})


@pytest.mark.parametrize(
    "events, outcome",
    [
        # The reply that ended the turn reached where the session compacts.
        (
            [*GATE, *COMPACTED, *_step({"text": "Waiting."})],
            (["At the gate."], "Waiting.", "stop"),
        ),
        ([*GATE, *COMPACTED, *_step()], (["At the gate."], "", "stop")),
        (
            [*GATE, *COMPACTED, *_step({"text": "Going on"}, reason="length")],
            (["At the gate."], "Going on", "length"),
        ),
        # A step with nothing to say ended the reply.
        (
            [
                *_step({"text": "At the gate."}, {"tool": "bash"}, reason="tool-calls"),
                *_step(),
                *COMPACTED,
                *_step({"text": "Waiting."}),
            ],
            (["At the gate."], "Waiting.", "stop"),
        ),
        # A step in the middle of the reply did.
        (
            [
                *_step({"text": "Fitting."}, reason="tool-calls"),
                *COMPACTED,
                *_step({"text": "Done."}),
            ],
            ([], "Done.", "stop"),
        ),
    ],
)
def test_a_reply_opencode_compacted_after_and_went_on_from_is_kept(
    tmp_path, workspace, events, outcome
):
    assert _run(tmp_path, workspace, events) == outcome


NODE = pytest.mark.skipif(not shutil.which("node"), reason="runs the plugin on Node")
# Runs the plugin in argv[2] on the events in argv[3], "resumed" starting a task that resumes ses_2, then hands it a task's result from ses_2.
DRIVE = """
import fs from "node:fs";
const [plugin, events] = process.argv.slice(2);
const hooks = await (await import(plugin)).default.server({ directory: "/" });
for (const event of JSON.parse(fs.readFileSync(events)))
  if (event === "resumed")
    await hooks["tool.execute.before"](
      { tool: "task", sessionID: "ses_1", callID: "call_1" },
      { args: { task_id: "ses_2" } },
    );
  else await hooks.event({ event });
const output = { output: "Done.", metadata: { sessionId: "ses_2" } };
await hooks["tool.execute.after"]({ tool: "task", callID: "call_1" }, output);
process.stdout.write(output.output);
"""


def _task_result(tmp_path, parts):
    """The result of a task the plugin hands the orchestrator once OpenCode
    has published `parts` of the subagent's session."""
    plugin = tmp_path / "af_opencode.mjs"
    plugin.write_text(runner.PLUGIN.read_text())
    (tmp_path / "drive.mjs").write_text(DRIVE)
    published = [
        part
        if part == "resumed"
        else {
            "type": "message.part.updated",
            "properties": {"part": {"sessionID": "ses_2", **part}},
        }
        for part in parts
    ]
    (tmp_path / "events.json").write_text(json.dumps(published))
    settings = {
        "records": str(tmp_path / "records.jsonl"),
        "tasks": str(tmp_path / "tasks"),
        "seats": 1,
        "images": {},
    }
    return subprocess.run(
        ["node", tmp_path / "drive.mjs", plugin, tmp_path / "events.json"],
        env={**os.environ, "AF_PLUGIN": json.dumps(settings)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _parts(text, reason="stop"):
    """The parts of one step, as OpenCode publishes them."""
    return [
        {"type": "step-start"},
        {"type": "text", "text": text, "time": {"end": 1}},
        {"type": "step-finish", "reason": reason},
    ]


@NODE
@pytest.mark.parametrize("reason, kept", [("stop", 1), ("tool-calls", 0)])
def test_a_subagents_reply_opencode_compacted_after_reaches_the_orchestrator_once(
    tmp_path, reason, kept
):
    compaction = {"type": "compaction", "id": "prt_1", "auto": True}
    result = _task_result(
        tmp_path,
        [
            *_parts("At the gate.", reason),
            compaction,
            *_parts("SUMMARY of the session"),
            # Published again with the tail OpenCode keeps.
            {**compaction, "tail_start_id": "msg_1"},
            *_parts("Done."),
        ],
    )

    assert result.startswith("Done.")
    assert result.count("At the gate.") == kept and "SUMMARY" not in result


@NODE
def test_a_subagents_reply_a_failed_task_left_reaches_the_task_resumed(tmp_path):
    result = _task_result(
        tmp_path,
        [
            *_parts("At the gate."),
            {"type": "compaction", "id": "prt_1", "auto": True},
            *_parts("SUMMARY of the session"),
            # The task failed as the subagent went on, and the orchestrator resumes it.
            "resumed",
            *_parts("Done."),
        ],
    )

    assert result.startswith("Done.") and result.count("At the gate.") == 1


# Runs the plugin in argv[2] on the steps in argv[3]: an event, a request's headers or messages, or the orchestrator resuming ses_2; prints what each request's messages carry in place of OpenCode's prompt to go on.
NUDGE = """
import fs from "node:fs";
const [plugin, steps] = process.argv.slice(2);
const hooks = await (await import(plugin)).default.server({ directory: "/" });
for (const step of JSON.parse(fs.readFileSync(steps)))
  if (step.messages) {
    await hooks["experimental.chat.messages.transform"]({}, step);
    for (const { parts } of step.messages)
      for (const part of parts)
        if (part.metadata?.compaction_continue) console.log(JSON.stringify(part.text));
  } else if (step.request)
    await hooks["chat.headers"]({ sessionID: step.session, agent: step.request }, { headers: {} });
  else if ("resumed" in step)
    await hooks["tool.execute.before"](
      { tool: "task", sessionID: "ses_1", callID: "call_1" },
      { args: { task_id: "ses_2", prompt: step.resumed } },
    );
  else await hooks.event({ event: step });
"""
GO_ON = "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."
TASK = "Write Phases 1 and 2 only."


def _nudged(tmp_path, steps):
    """What the requests among `steps` carry in place of OpenCode's prompt to
    go on, the plugin keeping its subagents' tasks in tmp_path/tasks."""
    plugin = tmp_path / "af_opencode.mjs"
    plugin.write_text(runner.PLUGIN.read_text())
    (tmp_path / "nudge.mjs").write_text(NUDGE)
    (tmp_path / "steps.json").write_text(json.dumps(steps))
    settings = {
        "records": str(tmp_path / "records.jsonl"),
        "tasks": str(tmp_path / "tasks"),
        "seats": 8,
        "images": {},
    }
    printed = subprocess.run(
        ["node", tmp_path / "nudge.mjs", plugin, tmp_path / "steps.json"],
        env={**os.environ, "AF_PLUGIN": json.dumps(settings)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [json.loads(line) for line in printed.splitlines()]


def _published(session, *parts):
    return [
        {
            "type": "message.part.updated",
            "properties": {"part": {"sessionID": session, **part}},
        }
        for part in parts
    ]


def _compacted(session, reason, n=1, said="At the gate.", overflow=False, died=False):
    """A step of `session` that said `said` and ended for `reason`, OpenCode's
    compaction after it, the summary, and the request that goes on with
    OpenCode's prompt to go on; after `died`, as a later turn sees them once
    the turn that began the compaction died before its summary."""
    compaction = {
        "type": "compaction",
        "id": f"prt_c{n}",
        "auto": True,
        "overflow": overflow,
    }
    go_on = {
        "id": f"prt_g{n}",
        "type": "text",
        "synthetic": True,
        "text": GO_ON,
        "metadata": {"compaction_continue": True},
    }
    step = (
        _parts(said, reason)
        if said
        else [{"type": "step-start"}, {"type": "step-finish", "reason": reason}]
    )
    summary = "SUMMARY: Phase 3 is next."
    return [
        *([] if died else _published(session, *step, compaction)),
        {"request": "compaction", "session": session},
        *_published(
            session, *_parts(summary), {**compaction, "tail_start_id": "msg_1"}, go_on
        ),
        {
            "request": "executor" if session == "ses_2" else "orchestrator",
            "session": session,
        },
        {
            "messages": [
                {"info": {"sessionID": session, "role": "user"}, "parts": [compaction]},
                {
                    "info": {"sessionID": session, "role": "assistant"},
                    "parts": [{"type": "text", "text": summary}],
                },
                {"info": {"sessionID": session, "role": "user"}, "parts": [go_on]},
            ]
        },
    ]


SUBAGENT = {
    "type": "session.created",
    "properties": {"info": {"id": "ses_2", "parentID": "ses_1"}},
}
# The subagent's first request, its task the whole context.
STARTED = {
    "messages": [
        {
            "info": {"sessionID": "ses_2", "role": "user"},
            "parts": [{"type": "text", "text": TASK}],
        }
    ]
}
MID_WORK = [
    {"reason": "tool-calls"},
    # A reply cut at its output limit goes on.
    {"reason": "length"},
    {"reason": "stop", "overflow": True},
    {"reason": "stop", "died": True},
]


@NODE
@pytest.mark.parametrize("how", MID_WORK)
def test_a_subagent_goes_on_after_a_compaction_with_its_task_only(tmp_path, how):
    (nudged,) = _nudged(tmp_path, [SUBAGENT, STARTED, *_compacted("ses_2", **how)])

    assert GO_ON not in nudged and nudged.endswith(TASK)


@NODE
@pytest.mark.parametrize("how", MID_WORK)
def test_the_orchestrator_goes_on_from_a_compaction_in_its_work_as_opencode_says(
    tmp_path, how
):
    assert _nudged(tmp_path, _compacted("ses_1", **how)) == [GO_ON]


@NODE
@pytest.mark.parametrize("session", ["ses_1", "ses_2"])
def test_an_agent_compacted_after_the_reply_that_ended_its_turn_ends_it_again(
    tmp_path, session
):
    (nudged,) = _nudged(tmp_path, [SUBAGENT, STARTED, *_compacted(session, "stop")])

    assert GO_ON not in nudged and TASK not in nudged


@NODE
def test_a_subagent_whose_last_step_said_nothing_goes_on_to_its_result(tmp_path):
    said = _published("ses_2", *_parts("Writing the result.", "tool-calls"))
    (nudged,) = _nudged(
        tmp_path, [SUBAGENT, STARTED, *said, *_compacted("ses_2", "stop", said="")]
    )

    assert nudged.endswith(TASK)


@NODE
def test_each_compaction_is_nudged_by_the_step_before_it(tmp_path):
    first, second = _nudged(
        tmp_path,
        [
            SUBAGENT,
            STARTED,
            *_compacted("ses_2", "stop", 1),
            *_compacted("ses_2", "tool-calls", 2),
        ],
    )

    assert TASK not in first and second.endswith(TASK)


@NODE
def test_a_later_turn_finds_the_task_and_what_the_subagent_was_resumed_with(tmp_path):
    _nudged(tmp_path, [SUBAGENT, STARTED])

    (nudged,) = _nudged(
        tmp_path, [{"resumed": "Fix finding A."}, *_compacted("ses_2", "tool-calls")]
    )
    assert TASK in nudged and nudged.endswith("Fix finding A.")


# Runs the plugin in argv[2] as agent argv[4] of one session, through the steps in argv[3]: reading a file, or a webfetch returning a PNG of so many bytes.
IMAGES = """
import fs from "node:fs";
const [plugin, steps, agent] = process.argv.slice(2);
const hooks = await (await import(plugin)).default.server({ directory: "/" });
await hooks["chat.headers"]({ sessionID: "ses_1", agent }, { headers: {} });
const outcomes = [];
for (const [i, step] of JSON.parse(fs.readFileSync(steps)).entries()) {
  const call = { sessionID: "ses_1", callID: `call_${i}` };
  if (step.read)
    try {
      const args = { filePath: step.read };
      await hooks["tool.execute.before"]({ tool: "read", ...call }, { args });
      outcomes.push("shown");
    } catch (refusal) {
      outcomes.push(refusal.message);
    }
  else {
    const url = `data:image/png;base64,${Buffer.alloc(step.fetched).toString("base64")}`;
    const output = { output: "", attachments: [{ mime: "image/png", url }] };
    await hooks["tool.execute.after"]({ tool: "webfetch", ...call }, output);
    outcomes.push(output.attachments.length ? "shown" : output.output);
  }
}
process.stdout.write(JSON.stringify(outcomes));
"""


def _shown(tmp_path, steps, images, most):
    """What the plugin does with each image a plot validator reads or fetches:
    "shown", or why not."""
    plugin = tmp_path / "af_opencode.mjs"
    plugin.write_text(runner.PLUGIN.read_text())
    (tmp_path / "images.mjs").write_text(IMAGES)
    (tmp_path / "steps.json").write_text(json.dumps(steps))
    settings = {
        "records": str(tmp_path / "records.jsonl"),
        "tasks": str(tmp_path / "tasks"),
        "seats": 1,
        "images": {"plot_validator": images},
        "image_bytes": {"plot_validator": most},
    }
    return json.loads(
        subprocess.run(
            ["node", tmp_path / "images.mjs", plugin, tmp_path / "steps.json"]
            + ["plot_validator"],
            env={**os.environ, "AF_PLUGIN": json.dumps(settings)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


@NODE
def test_an_image_too_large_for_a_request_of_its_role_is_refused(tmp_path):
    small, large = tmp_path / "small.png", tmp_path / "large.jpg"
    small.write_bytes(b"\x89PNG" + bytes(996))
    large.write_bytes(b"\xff\xd8\xff" + bytes(998))
    steps = [
        {"read": str(large)},
        {"read": str(small)},
        {"fetched": 1001},
        {"fetched": 1000},
        # Neither refusal took up one of the session's images.
        {"read": str(small)},
    ]

    shown = _shown(tmp_path, steps, images=3, most=1000)

    assert [outcome == "shown" for outcome in shown] == [False, True, False, True, True]


SITE = ".pixi/envs/default/lib/python3.12/site-packages"
# Where a rebuild on another Python leaves an environment's packages.
MOVED = ".pixi/envs/default/lib/python3.11/site-packages"
# Runs the plugin in argv[2] on the analysis in argv[3], for argv[4]'s steps: "bash" as a command starts, or an environment's rebuild, the directory to empty.
BUILT = """
import fs from "node:fs";
const [plugin, directory, steps] = process.argv.slice(2);
const hooks = await (await import(plugin)).default.server({ directory });
for (const step of JSON.parse(fs.readFileSync(steps)))
  if (step === "bash")
    await hooks["tool.execute.before"](
      { tool: "bash", sessionID: "ses_1", callID: "call_1" },
      { args: { command: "true" } },
    );
  else {
    fs.rmSync(step, { recursive: true, force: true });
    fs.mkdirSync(step, { recursive: true });
  }
"""


def _run_plugin(tmp_path, steps, helpers=None):
    """The analysis the plugin ran on, and the records it wrote, once it has
    gone through `steps`."""
    plugin, work = tmp_path / "af_opencode.mjs", tmp_path / "work"
    plugin.write_text(runner.PLUGIN.read_text())
    (tmp_path / "built.mjs").write_text(BUILT)
    (tmp_path / "steps.json").write_text(
        json.dumps([step if step == "bash" else str(work / step) for step in steps])
    )
    (tmp_path / "local").mkdir(exist_ok=True)
    settings = {
        "records": str(tmp_path / "records.jsonl"),
        "tasks": str(tmp_path / "tasks"),
        "helpers": str(helpers or runner._helpers(tmp_path / "local")),
        "hook": runner.HOOK,
        "seats": 1,
        "images": {},
    }
    (work / SITE).mkdir(parents=True, exist_ok=True)
    # Conda's alias of the Python directory.
    alias = work / SITE.replace("python3.12", "python3.1")
    if not alias.parent.is_symlink():
        alias.parent.symlink_to("python3.12")
    subprocess.run(
        ["node", tmp_path / "built.mjs", plugin, work, tmp_path / "steps.json"],
        env={**os.environ, "AF_PLUGIN": json.dumps(settings)},
        capture_output=True,
        text=True,
        check=True,
    )
    return work, [
        record
        for line in (tmp_path / "records.jsonl").read_text().splitlines()
        if (record := json.loads(line))["kind"] == "helpers"
    ]


@NODE
def test_a_turn_puts_the_helpers_into_the_environment(tmp_path):
    work, (record,) = _run_plugin(tmp_path, [])

    assert record == {
        "t": record["t"],
        "kind": "helpers",
        "packages": str(work / SITE),
        "error": None,
    }
    assert sorted(path.name for path in (work / SITE).iterdir()) == sorted(
        path.name for path in (tmp_path / "local/helpers").iterdir()
    )


@NODE
def test_a_command_finds_the_helpers_a_rebuild_took_out(tmp_path):
    work, records = _run_plugin(tmp_path, [MOVED, "bash", "bash"])

    assert [record["packages"] for record in records] == [
        str(work / SITE),
        str(work / MOVED),
    ]
    assert (work / MOVED / runner.HOOK).is_file()
    assert (work / MOVED / "af_dask.py").is_file()


@NODE
def test_a_command_leaves_helpers_that_are_there_and_a_turn_renews_them(tmp_path):
    work, _ = _run_plugin(tmp_path, ["bash"])
    (work / SITE / "af_dask.py").write_text("older")

    _run_plugin(tmp_path, ["bash"], tmp_path / "local/helpers")

    assert "account_token" in (work / SITE / "af_dask.py").read_text()


@NODE
def test_helpers_that_cannot_be_put_there_leave_the_command_to_run(tmp_path):
    _, (record, again) = _run_plugin(tmp_path, [SITE, "bash"], tmp_path / "no-helpers")

    assert "ENOENT" in again["error"]
    assert record["error"] == again["error"]


def test_a_turn_logs_where_the_helpers_went_and_why_they_did_not(
    tmp_path, workspace, capsys
):
    records = (
        {"kind": "loaded"},
        {"kind": "helpers", "packages": "/env/a", "error": None},
        {"kind": "helpers", "packages": "/env/b", "error": "EACCES"},
    )

    _run(tmp_path, workspace, _step({"text": "done"}), records=records)

    logged = capsys.readouterr().out
    assert "helpers in /env/a: put there" in logged
    assert "helpers in /env/b: EACCES" in logged


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(runner, "POLL", 0.05)
    monkeypatch.setattr(runner, "LOADED", 1)
    monkeypatch.setattr(runner, "STALL", 1)
    monkeypatch.setattr(runner, "GRACE", 1)


@pytest.mark.parametrize(
    "records, why", [((), "did not load"), (({"kind": "loaded"},), "said nothing")]
)
def test_a_turn_whose_plugin_never_loads_or_that_falls_silent_is_ended(
    tmp_path, workspace, quick, records, why
):
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=why) as raised:
        _run(tmp_path, workspace, _step(reason="tool-calls"), records=records, after=60)
    assert time.monotonic() - started < 10
    assert _retried(raised)


def test_a_turn_whose_plugin_notes_what_starts_is_not_silent(
    tmp_path, workspace, quick
):
    """As while a subagent waits for its model, then for its command."""
    script = _fake_opencode(tmp_path, _step({"text": "done"}), records=())
    noting = (
        "import json, os, time\n"
        "path = json.loads(os.environ['AF_PLUGIN'])['records']\n"
        "for kind in ['loaded'] + ['beat'] * 8:\n"
        "    open(path, 'a').write(json.dumps({'kind': kind}) + '\\n')\n"
        "    time.sleep(0.3)\n"
    )
    Path(script[1]).write_text(noting + Path(script[1]).read_text())
    home = workspace.parents[2]
    started = time.monotonic()

    said = runner._opencode(script, "", workspace, _plugin(home), home)

    assert said == ([], "done", "stop")
    assert time.monotonic() - started > 2 * runner.STALL


PROC = pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="reads /proc")


@pytest.mark.parametrize(
    "detached, code",
    [
        # Even what holds OpenCode's output open after it has exited.
        (False, 0),
        # The bash tool starts each command in a session of its own.
        pytest.param(True, 0, marks=PROC),
        pytest.param(True, 1, marks=PROC),
    ],
)
def test_a_turn_ends_what_it_started(tmp_path, workspace, monkeypatch, detached, code):
    monkeypatch.setattr(runner, "POLL", 0.05)
    beat = tmp_path / "beat"
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib, time\n"
        "while True:\n"
        f"    pathlib.Path({str(beat)!r}).write_text(str(time.time()))\n"
        "    time.sleep(0.05)\n"
    )
    script = _fake_opencode(tmp_path, _step({"text": "done"}), code=code)
    started = (
        "import subprocess, time\n"
        f"subprocess.Popen([{sys.executable!r}, {str(child)!r}], start_new_session={detached}, "
        # A detached command does not hold OpenCode's output: one left running fails the test.
        f"stdout=subprocess.DEVNULL if {detached} else None)\n"
        f"while not __import__('os').path.exists({str(beat)!r}): time.sleep(0.01)\n"
    )
    Path(script[1]).write_text(started + Path(script[1]).read_text())
    home = workspace.parents[2]

    try:
        runner._opencode(script, "", workspace, _plugin(home), home)
    except RuntimeError:
        assert code
    else:
        assert not code

    last = beat.read_text()
    time.sleep(0.3)
    assert beat.read_text() == last


@pytest.fixture
def agent(workspace, monkeypatch):
    """`_agent` with the proxy's roles, and each turn's outcome taken in turn
    from `outcomes`; the first turn opens session ses_1, and every turn takes
    its prompt and writes into OpenCode's configuration and the analysis's as
    an agent might."""
    state = types.SimpleNamespace(outcomes=[], prompts=[], configs=[], loaded=[])

    def opencode(command, prompt, work, env, home, taken):
        state.prompts.append(prompt)
        state.configs.append(
            [p.name for p in work.iterdir() if p.name in runner.CONFIGS]
        )
        plugin = Path(env["OPENCODE_CONFIG_DIR"]) / "plugin" / runner.PLUGIN.name
        state.loaded.append(plugin.read_text())
        plugin.chmod(0o644)
        plugin.write_text("// edited by an agent")
        (work / "AGENTS.md").write_text("planted by an agent")
        (home / "opencode" / "session").write_text("ses_1")
        if taken:
            taken()
        return state.outcomes.pop(0)

    monkeypatch.setattr(runner, "_roles", lambda: ROLES)
    monkeypatch.setattr(runner, "_opencode", opencode)
    monkeypatch.setenv(runner.PROXY, "http://llm-proxy:8080")
    return state


def test_a_reply_cut_off_at_its_output_limit_is_resumed(workspace, agent):
    home = workspace.parents[2]
    (workspace / "opencode.json").write_text("{}")
    agent.outcomes = [([], "half a", "length"), ([], "At the human gate.", "stop")]

    assert runner._agent(workspace, home, "", home / "told") == "At the human gate."

    assert agent.prompts == [runner.UNATTENDED + "search for ZZ", runner.INTERRUPTED]
    assert agent.configs == [[], []]
    # Each turn on OpenCode's configuration as the runner wrote it.
    assert agent.loaded == [runner.PLUGIN.read_text()] * 2
    assert not (home / "told").exists()


def test_a_reply_opencode_went_on_from_outlasts_a_resume(workspace, agent):
    home = workspace.parents[2]
    agent.outcomes = [(["At the gate."], "cut", "length"), ([], "Waiting.", "stop")]

    said = runner._agent(workspace, home, "", home / "told")

    assert said == "At the gate.\n\nWaiting."


def test_replies_that_keep_running_out_of_output_end_the_run(workspace, agent):
    home = workspace.parents[2]
    agent.outcomes = [([], "cut", "length")] * (runner.RESUMES + 1)

    with pytest.raises(RuntimeError, match="output tokens"):
        runner._agent(workspace, home, "APPROVE", home / "told")

    assert len(agent.prompts) == runner.RESUMES + 1
    assert agent.prompts[1] == runner.INTERRUPTED
    assert (home / "told").read_text() == "APPROVE"


# --- the task ---


@pytest.fixture
def stretch(root, monkeypatch):
    """`advance` with the setup done, the agent recorded, and no real pauses."""
    work = root / "ws" / "jfc" / "analyses" / "zz"
    work.mkdir(parents=True)
    (work / "prompt.md").write_text("search for ZZ")
    state = types.SimpleNamespace(
        attempt_number=0,
        action=types.SimpleNamespace(run_name="ws"),
        prompts=[],
        slept=[],
    )

    def opencode(command, prompt, work, env, home, taken):
        state.prompts.append(prompt)
        (home / "opencode" / "session").write_text("ses_1")
        if taken:
            taken()
        return [], "At the human gate.", "stop"

    monkeypatch.setattr(runner.flyte, "ctx", lambda: state, raising=False)
    monkeypatch.setattr(runner, "_prepare", lambda home, analysis: work)
    monkeypatch.setattr(runner, "_roles", lambda: ROLES)
    monkeypatch.setattr(runner, "_opencode", opencode)
    monkeypatch.setattr(runner.time, "sleep", state.slept.append)
    monkeypatch.setenv(runner.PROXY, "http://llm-proxy:8080")
    monkeypatch.setenv(runner.TOKEN, "client token")
    (root / "token").write_text("stand-in")
    monkeypatch.setenv(clusters.TOKEN_FILE, str(root / "token"))
    return state


@pytest.mark.parametrize("unset", ["AF_LLM_PROXY", "AF_LLM_TOKEN"])
def test_advance_refuses_to_start_without_the_llm_proxy(
    root, stretch, monkeypatch, unset
):
    monkeypatch.delenv(unset)

    with pytest.raises(runner.flyte.errors.NonRecoverableError, match=unset):
        runner.advance(ANALYSIS, "ws", "")
    assert stretch.prompts == []


@pytest.mark.parametrize("unset", [True, False])
def test_advance_refuses_to_start_without_its_accounts_token(
    root, stretch, monkeypatch, unset
):
    if unset:
        monkeypatch.delenv(clusters.TOKEN_FILE)
    else:
        (root / "token").unlink()

    with pytest.raises(
        runner.flyte.errors.NonRecoverableError, match=clusters.TOKEN_FILE
    ):
        runner.advance(ANALYSIS, "ws", "")
    assert stretch.prompts == []


def test_advance_returns_and_keeps_what_the_agent_said(root, stretch):
    assert runner.advance(ANALYSIS, "ws", "") == "At the human gate."
    assert (root / "ws/said.md").read_text() == "At the human gate."
    assert stretch.slept == []


def test_each_turn_reads_the_runners_facility_rules(root, stretch, monkeypatch):
    read = []

    def opencode(command, prompt, work, env, home, taken):
        facility, _ = json.loads(env["OPENCODE_CONFIG_CONTENT"])["instructions"]
        read.append(Path(facility).read_text())
        return [], "", "stop"

    monkeypatch.setattr(runner, "_opencode", opencode)
    runner.advance(ANALYSIS, "ws", "")

    assert read == [
        runner.FACILITY.format(
            cores=runner.CORES,
            hours=runner.COMMAND_HOURS,
            minutes=runner.COMMAND_MINUTES,
            python=runner.PYTHON,
            root=runner.ROOT,
        )
    ]


def test_advance_hands_a_reply_to_the_agent_once(root, stretch):
    runner.advance(ANALYSIS, "ws", "")
    stretch.action.run_name = "run-2"

    runner.advance(ANALYSIS, "ws", "REGRESS(2)")
    runner.advance(ANALYSIS, "ws", "REGRESS(2)")  # run-2 retried

    assert stretch.prompts[1:] == [
        runner.REPLY.format(reply="REGRESS(2)"),
        runner.INTERRUPTED,
    ]
    assert (root / "ws/replies/run-2").read_text() == "REGRESS(2)"


@pytest.mark.parametrize("fails", ["_roles", "_opencode"])
def test_a_reply_opencode_never_took_reaches_the_agent_on_the_retry(
    root, stretch, monkeypatch, fails
):
    runner.advance(ANALYSIS, "ws", "")
    stretch.action.run_name = "run-2"
    working = getattr(runner, fails)
    monkeypatch.setattr(runner, fails, _failing(RuntimeError("proxy restarting")))

    with pytest.raises(RuntimeError, match="restarting"):
        runner.advance(ANALYSIS, "ws", "APPROVE")
    monkeypatch.setattr(runner, fails, working)
    runner.advance(ANALYSIS, "ws", "APPROVE")  # run-2 retried

    assert stretch.prompts[1:] == [runner.REPLY.format(reply="APPROVE")]


@pytest.mark.parametrize("reply", ["", "APPROVE"])
def test_advance_refuses_a_workspace_that_is_neither_its_own_nor_there(
    root, stretch, reply
):
    with pytest.raises(runner.flyte.errors.NonRecoverableError):
        runner.advance(ANALYSIS, "typo", reply)
    assert stretch.prompts == [] and not (root / "typo").exists()


def test_advance_runs_the_agent_under_the_workspace_lock(root, stretch, monkeypatch):
    def opencode(command, prompt, work, env, home, taken):
        with open(root / "ws/lock") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return [], "", "stop"

    monkeypatch.setattr(runner, "_opencode", opencode)
    runner.advance(ANALYSIS, "ws", "")


def _failing(error):
    def opencode(*turn):
        raise error

    return opencode


def test_advance_waits_after_a_failed_turn_longer_each_time(stretch, monkeypatch):
    monkeypatch.setattr(runner, "_opencode", _failing(RuntimeError("proxy down")))

    for stretch.attempt_number in (0, 1, 4, runner.RETRIES):
        with pytest.raises(RuntimeError, match="proxy down"):
            runner.advance(ANALYSIS, "ws", "")

    assert stretch.slept == [15 * 60, 30 * 60, 240 * 60]


def test_advance_does_not_wait_on_a_failure_no_retry_follows(stretch, monkeypatch):
    final = runner.flyte.errors.NonRecoverableError(
        "opencode: a bearer token is required"
    )
    monkeypatch.setattr(runner, "_opencode", _failing(final))

    with pytest.raises(runner.flyte.errors.NonRecoverableError):
        runner.advance(ANALYSIS, "ws", "")

    assert stretch.slept == []
