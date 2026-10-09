"""The Python the Dask Gateway execs (gateway.extraConfig.config)."""

import sys
import types

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestServer
from common import REPO, ConfigSink

VALUES = REPO / "apps" / "dask-gateway" / "values.yaml"
BASE_DN = "ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"


@pytest.fixture(scope="module")
def code() -> str:
    return yaml.safe_load(VALUES.read_text())["gateway"]["extraConfig"]["config"]


class Unauthorized(Exception):
    pass


class SimpleAuthenticator:
    async def authenticate(self, request):
        return "basic"


@pytest.fixture
def config(monkeypatch, code):
    """Exec the embedded config against stub dask_gateway_server modules."""
    options = types.ModuleType("dask_gateway_server.options")
    for name in ("Options", "Integer", "Float", "Mapping", "String", "Select"):
        setattr(options, name, lambda *args, **kwargs: None)
    kubernetes = types.ModuleType("dask_gateway_server.backends.kubernetes")
    kubernetes.KubeBackend = type("KubeBackend", (), {"start_cluster": None})
    base = types.ModuleType("dask_gateway_server.backends.base")
    base.PublicException = Exception
    auth = types.ModuleType("dask_gateway_server.auth")
    auth.SimpleAuthenticator = SimpleAuthenticator
    auth.unauthorized = Unauthorized
    package = types.ModuleType("dask_gateway_server")
    package.models = types.ModuleType("dask_gateway_server.models")
    package.models.User = lambda name: name
    for module in (options, kubernetes, base, auth, package):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    ns = {"c": ConfigSink()}
    exec(compile(code, f"{VALUES}:gateway.extraConfig.config", "exec"), ns)
    return ns


@pytest.fixture
def options_handler(config):
    return config["options_handler"]


async def authenticate(config, header, hub_status=200, owner=None):
    async def whoami(request):
        assert request.headers["Authorization"] == "token secret"
        return web.json_response(owner, status=hub_status)

    hub = web.Application()
    hub.router.add_get("/hub/api/user", whoami)
    async with TestServer(hub) as server:
        authenticator = config["HubTokenAuthenticator"]()
        authenticator.hub_api_url = str(server.make_url("/hub/api"))
        return await authenticator.authenticate(
            types.SimpleNamespace(headers={"Authorization": header} if header else {})
        )


async def test_hub_token_names_its_owner(config):
    owner = {"kind": "user", "name": "someone-cern"}
    user = await authenticate(config, "jupyterhub secret", owner=owner)
    assert user == "someone-cern"
    assert (
        config["c"]["DaskGateway"]["authenticator_class"]
        is config["HubTokenAuthenticator"]
    )


@pytest.mark.parametrize(
    "hub_status, owner",
    [(403, {}), (200, {"kind": "service", "name": "prometheus"})],
)
async def test_token_the_hub_rejects_or_a_service_owns_is_refused(
    config, hub_status, owner
):
    with pytest.raises(Unauthorized):
        await authenticate(config, "jupyterhub secret", hub_status, owner)


async def test_hub_failure_is_not_an_authentication_failure(config):
    with pytest.raises(web.HTTPBadGateway):
        await authenticate(config, "jupyterhub secret", 503, {})


@pytest.mark.parametrize("header", ["Basic c29tZW9uZTo=", None])
async def test_other_schemes_go_to_basic(config, header):
    assert await authenticate(config, header) == "basic"


def build_environment(project, *packages):
    """A pixi project whose default environment holds `packages`; returns the
    environment's prefix."""
    prefix = project / ".pixi" / "envs" / "default"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "python").touch()
    (project / "pixi.toml").touch()
    for package in packages:
        (prefix / "lib" / "python3.12" / "site-packages" / package).mkdir(parents=True)
    return prefix


@pytest.fixture
def conda_env(tmp_path) -> str:
    return str(build_environment(tmp_path, "distributed", "prometheus_client"))


def test_env_names_kubernetes_rejects_are_dropped(options_handler, conda_env):
    options = types.SimpleNamespace(
        env={
            "PATH": "/usr/bin",
            "X509_USER_PROXY": "/work/users/someone/x509up",
            "my.env-name": "kept",
            "BASH_FUNC_which%%": "() {  ( alias; eval ${which_declare} ) | /usr/bin/which $@\n}",
            "1_LEADING_DIGIT": "dropped",
        },
        conda_env=conda_env,
        pixi_project="",
        pixi_env="default",
        worker_cores=1,
        worker_memory=4,
    )
    config = options_handler(options, types.SimpleNamespace(name="jovyan"))
    environment = config["environment"]
    assert "BASH_FUNC_which%%" not in environment
    assert "1_LEADING_DIGIT" not in environment
    assert environment["X509_USER_PROXY"] == "/work/users/someone/x509up"
    assert environment["my.env-name"] == "kept"


def request_options(conda_env="", pixi_project="", **env):
    return types.SimpleNamespace(
        env={"PATH": "/usr/bin", **env},
        conda_env=conda_env,
        pixi_project=pixi_project,
        pixi_env="default",
        worker_cores=1,
        worker_memory=4,
    )


@pytest.mark.parametrize("option", ["conda_env", "pixi_project"])
def test_environment_without_prometheus_client_is_refused(
    options_handler, tmp_path, option
):
    prefix = build_environment(tmp_path, "distributed")
    named = {"conda_env": prefix, "pixi_project": tmp_path}[option]
    options = request_options(**{option: str(named)})
    with pytest.raises(ValueError, match="prometheus_client is not installed"):
        options_handler(options, types.SimpleNamespace(name="jovyan"))


def test_pixi_project_runs_its_environment(options_handler, tmp_path):
    prefix = build_environment(tmp_path, "distributed", "prometheus_client")
    options = request_options(pixi_project=str(tmp_path))
    cluster = options_handler(options, types.SimpleNamespace(name="jovyan"))
    assert cluster["scheduler_cmd"] == [f"{prefix}/bin/dask", "scheduler"]


def test_pods_run_as_the_user_without_changing_the_shared_config(config, conda_env):
    """The uid and the user label are returned for this cluster; the config
    every later request starts from keeps what the chart put there."""
    shared = config["c"].KubeClusterConfig
    shared["scheduler_extra_pod_config"] = {"nodeSelector": {"cms-af-prod": "true"}}
    shared["worker_extra_pod_config"] = {"volumes": ["cvmfs"]}
    user = types.SimpleNamespace(name="someone-cern")
    options = request_options(conda_env, NB_UID="4001", NB_GID="4002")

    cluster = config["options_handler"](options, user)

    run_as = {"runAsUser": 4001, "runAsGroup": 4002}
    assert cluster["scheduler_extra_pod_config"] == {
        "nodeSelector": {"cms-af-prod": "true"},
        "securityContext": run_as,
    }
    assert cluster["worker_extra_pod_config"] == {
        "volumes": ["cvmfs"],
        "securityContext": run_as,
    }
    assert cluster["scheduler_extra_pod_labels"] == {"user": "someone-cern"}
    assert cluster["worker_extra_pod_labels"] == {"user": "someone-cern"}
    assert shared["scheduler_extra_pod_config"] == {
        "nodeSelector": {"cms-af-prod": "true"}
    }
    assert shared["worker_extra_pod_config"] == {"volumes": ["cvmfs"]}


async def cluster_handler(config, username, lookup):
    """The handler `cluster_options` builds for one user, and where the lookup ran."""
    import threading

    threads = []

    def ldap_lookup(name):
        threads.append(threading.current_thread() is threading.main_thread())
        return lookup(name)

    config["ldap_lookup"] = ldap_lookup
    config["Options"] = lambda *fields, handler: handler
    user = types.SimpleNamespace(name=username)
    return await config["cluster_options"](user), user, threads


async def test_purdue_uid_is_looked_up_off_the_event_loop(config, conda_env):
    handler, user, threads = await cluster_handler(
        config, "someone", lambda name: (5555, 6666)
    )
    cluster = handler(request_options(conda_env), user)
    assert threads == [False]
    assert cluster["worker_extra_pod_config"]["securityContext"] == {
        "runAsUser": 5555,
        "runAsGroup": 6666,
    }


async def test_failed_lookup_fails_the_cluster_not_the_options_listing(
    config, conda_env
):
    def missing(name):
        raise ValueError("no LDAP entry for " + name)

    handler, user, _ = await cluster_handler(config, "someone", missing)
    with pytest.raises(ValueError, match="no LDAP entry for someone"):
        handler(request_options(conda_env), user)


@pytest.mark.parametrize("username", ["jovyan", "someone-cern", "someone-fnal"])
async def test_accounts_outside_purdue_ldap_are_not_looked_up(
    config, conda_env, username
):
    handler, user, threads = await cluster_handler(config, username, None)
    cluster = handler(request_options(conda_env, NB_UID="4001", NB_GID="4002"), user)
    assert threads == []
    expected = 1000 if username == "jovyan" else 4001
    assert (
        cluster["worker_extra_pod_config"]["securityContext"]["runAsUser"] == expected
    )


def test_clusters_report_to_the_gateway_inside_the_cluster(config):
    """The chart points schedulers at the gateway's Service; an override here
    would route their heartbeats elsewhere."""
    assert "api_url" not in config["c"].get("KubeBackend", {})


def test_lookup_reads_the_dn_at_base_scope(code):
    assert 'search_base = "uid={0},{1}".format(username, baseDN)' in code
    assert "search_scope = BASE" in code


def test_lookup_falls_back_to_a_search_and_reports_a_miss(code):
    """An account not at its own DN is still resolved, and a genuine miss
    raises instead of IndexError-ing on entries[0]."""
    assert '"(uid={0}*)".format(username)' in code
    assert "search_scope = SUBTREE" in code
    assert 'raise ValueError("no LDAP entry for " + username)' in code
    assert "[u'entries'][0]" not in code


def test_lookup_targets_geddes_auth(code):
    assert 'url = "geddes-auth.rcac.purdue.edu"' in code
    assert f'baseDN = "{BASE_DN}"' in code
