"""The Python the Dask Gateway execs (gateway.extraConfig.config)."""

import base64
import copy
import json
import logging
import re
import sys
import types

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestServer
from common import (
    REFUSED_ACCOUNTS,
    REPO,
    ConfigSink,
    account,
    load_script,
    mount_configmap,
)
from multidict import CIMultiDict

VALUES = REPO / "apps" / "dask-gateway" / "values.yaml"
RELEASE = REPO / "apps" / "dask-gateway" / "helmrelease.yaml"
RAY_GATEWAY = REPO / "apps" / "ray-train" / "gateway.py"
BASE_DN = "ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu"
# What Kubernetes takes as a label's value.
LABEL_VALUE = re.compile(r"(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?")
# A pod's address, as the cluster's pods have them.
POD = "10.5.2.17"


@pytest.fixture(scope="module")
def code() -> str:
    return yaml.safe_load(VALUES.read_text())["gateway"]["extraConfig"]["config"]


class Unauthorized(Exception):
    pass


class SimpleAuthenticator:
    """dask-gateway-server 2024.1.0's: a request's cookie, while the cache holds
    its user, stands in for the credentials that signed them in."""

    cookie_name = "dask-gateway-cookie"

    def __init__(self):
        self.cache = {}

    async def authenticate_and_handle(self, request, handler):
        cookie = request.cookies.get(self.cookie_name)
        if cookie in self.cache:
            request["user"] = self.cache[cookie]
            return await handler(request)
        request["user"] = user = await self.authenticate(request)
        response = await handler(request)
        cookie = next((k for k, v in self.cache.items() if v.name == user.name), None)
        cookie = cookie or f"cookie-{len(self.cache)}"
        self.cache[cookie] = user
        response.set_cookie(self.cookie_name, cookie)
        return response

    async def authenticate(self, request):
        """The user a Basic header names, as dask-gateway-server's does."""
        scheme, _, encoded = request.headers.get("Authorization", "").partition(" ")
        if scheme != "Basic":
            raise Unauthorized("Basic")
        name = base64.b64decode(encoded).decode("ascii").split(":", 1)[0]
        return types.SimpleNamespace(name=name)


def basic(name):
    return "Basic " + base64.b64encode(f"{name}:".encode()).decode()


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
    package.models.User = lambda name: types.SimpleNamespace(name=name)
    for module in (options, kubernetes, base, auth, package):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    ns = {"c": ConfigSink()}
    exec(compile(code, f"{VALUES}:gateway.extraConfig.config", "exec"), ns)
    return ns


@pytest.fixture
def options_handler(config):
    return config["options_handler"]


async def authenticate(
    config, header, hub_status=200, owner=None, remote=POD, headers=()
):
    async def whoami(request):
        assert request.headers["Authorization"] == "token secret"
        return web.json_response(owner, status=hub_status)

    hub = web.Application()
    hub.router.add_get("/hub/api/user", whoami)
    async with TestServer(hub) as server:
        authenticator = config["HubTokenAuthenticator"]()
        authenticator.hub_api_url = str(server.make_url("/hub/api"))
        sent = CIMultiDict(headers)
        if header:
            sent["Authorization"] = header
        return await authenticator.authenticate(
            types.SimpleNamespace(headers=sent, remote=remote)
        )


async def test_hub_token_names_its_owner(config):
    owner = {"kind": "user", "name": "someone-cern"}
    user = await authenticate(config, "jupyterhub secret", owner=owner)
    assert user.name == "someone-cern"
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


async def test_other_schemes_go_to_basic(config):
    assert (await authenticate(config, basic("someone"))).name == "someone"
    with pytest.raises(Unauthorized):
        await authenticate(config, None)


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
    assert cluster["scheduler_cmd"][:2] == [f"{prefix}/bin/dask", "scheduler"]


def test_idle_schedulers_shut_themselves_down(options_handler, conda_env):
    cluster = options_handler(
        request_options(conda_env=conda_env), types.SimpleNamespace(name="jovyan")
    )
    cmd = cluster["scheduler_cmd"]
    assert cmd[cmd.index("--idle-timeout") + 1] == "3600s"


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


# --- workload accounts ---

TOKEN = "demo-token"
WORKLOAD = account()
WORKLOAD_ENV = "/demo-workload/run/jfc/.pixi/envs/default"


@pytest.fixture
def registry(config, tmp_path):
    """The af-accounts ConfigMap as kubelet mounts it, holding WORKLOAD and a second account."""
    directory = tmp_path / "af-accounts"
    directory.mkdir()
    config["ACCOUNTS_DIR"] = str(directory)
    other = account("other-workload", "other-token", 1300, 1300)
    mount_configmap(
        directory,
        "1",
        {
            "demo-workload.yaml": yaml.safe_dump(WORKLOAD),
            "other-workload.yaml": yaml.safe_dump(other),
        },
    )
    return directory


@pytest.mark.parametrize("remote", [POD, "10.5.77.254", "::ffff:10.5.2.17"])
async def test_an_account_token_from_a_pod_is_the_accounts_user(
    config, registry, remote
):
    user = await authenticate(config, f"Bearer {TOKEN}", remote=remote)
    assert user.name == "account:demo-workload"


@pytest.mark.parametrize(
    "header", [f"Bearer {TOKEN}x", "Bearer other", "Bearer ", f"bearer {TOKEN}"]
)
async def test_a_token_no_account_declares_is_refused(config, registry, header):
    with pytest.raises(Unauthorized):
        await authenticate(config, header)


async def test_without_the_registry_an_account_token_is_refused(config, tmp_path):
    config["ACCOUNTS_DIR"] = str(tmp_path / "missing")
    with pytest.raises(Unauthorized):
        await authenticate(config, f"Bearer {TOKEN}")


@pytest.mark.parametrize(
    "remote",
    [
        "128.211.160.5",
        "10.5.236.234",
        "10.5.2.0",
        "10.5.2.1",
        "10.4.2.17",
        "::1",
        "",
        None,
    ],
)
async def test_an_account_token_from_outside_the_pod_network_is_refused(
    config, registry, caplog, remote
):
    with caplog.at_level(logging.WARNING), pytest.raises(Unauthorized):
        await authenticate(config, f"Bearer {TOKEN}", remote=remote)
    assert "demo-workload refused: reason=outside-pod-network" in caplog.text
    assert TOKEN not in caplog.text and WORKLOAD["tokenSha256"] not in caplog.text


@pytest.mark.parametrize(
    "forwarded",
    [
        {"X-Forwarded-For": "128.211.160.5"},
        {"x-forwarded-for": POD},
        {"Forwarded": "for=10.5.2.18"},
        {"X-Real-Ip": POD},
    ],
)
async def test_a_forwarded_account_token_is_refused(
    config, registry, caplog, forwarded
):
    with caplog.at_level(logging.WARNING), pytest.raises(Unauthorized):
        await authenticate(config, f"Bearer {TOKEN}", headers=forwarded)
    assert "demo-workload refused: reason=forwarded" in caplog.text


@pytest.mark.parametrize("registry_present", [True, False])
@pytest.mark.parametrize("name", ["account:demo-workload", "someone:else", ":"])
async def test_no_basic_or_hub_user_has_a_colon_in_their_name(
    config, monkeypatch, tmp_path, caplog, registry_present, name
):
    """A Basic name never has one, as the credentials split at the first ':';
    the gateway refuses one all the same."""
    config["ACCOUNTS_DIR"] = str(tmp_path / "missing")
    if registry_present:
        config["ACCOUNTS_DIR"] = str(tmp_path)
        declare(tmp_path, **{"demo-workload.yaml": WORKLOAD})

    async def named(self, request):
        return types.SimpleNamespace(name=name)

    monkeypatch.setattr(SimpleAuthenticator, "authenticate", named)
    with caplog.at_level(logging.WARNING), pytest.raises(Unauthorized):
        await authenticate(config, basic("someone"))
    owner = {"kind": "user", "name": name}
    with caplog.at_level(logging.WARNING), pytest.raises(Unauthorized):
        await authenticate(config, "jupyterhub secret", owner=owner)
    assert caplog.text.count("user refused: reason=colon") == 2


async def test_no_basic_or_hub_user_has_a_declared_accounts_name(
    config, registry, caplog
):
    with caplog.at_level(logging.WARNING), pytest.raises(Unauthorized):
        await authenticate(config, basic("demo-workload"))
    owner = {"kind": "user", "name": "other-workload"}
    with caplog.at_level(logging.WARNING), pytest.raises(Unauthorized):
        await authenticate(config, "jupyterhub secret", owner=owner)
    assert "Basic user refused: reason=account-name account=demo-workload" in (
        caplog.text
    )
    assert "jupyterhub user refused: reason=account-name account=other-workload" in (
        caplog.text
    )


@pytest.mark.parametrize("name", ["someone", "demo-workload-old", "jovyan"])
async def test_users_of_other_names_sign_in_under_them(config, registry, name):
    assert (await authenticate(config, basic(name))).name == name
    owner = {"kind": "user", "name": name}
    assert (await authenticate(config, "jupyterhub secret", owner=owner)).name == name


async def test_a_token_that_is_no_utf8_is_refused_as_unknown(config, registry, caplog):
    """aiohttp hands a header's bytes that are no UTF-8 on as surrogates."""
    with caplog.at_level(logging.WARNING), pytest.raises(Unauthorized):
        await authenticate(config, "Bearer \udcff\udcfe")
    assert "workload account refused: reason=unknown-token" in caplog.text


class Request(dict):
    """What the authenticator reads of an aiohttp request; a handler's
    request["user"] is what it is signed in as."""

    def __init__(self, headers, remote=POD, cookies=None):
        super().__init__()
        self.headers = CIMultiDict(headers)
        self.remote = remote
        self.cookies = cookies or {}


async def signed_in(authenticator, request):
    """The user a request is handled as, and the cookie its answer sets."""

    async def handler(request):
        return web.Response(text=request["user"].name)

    response = await authenticator.authenticate_and_handle(request, handler)
    cookie = response.cookies.get(authenticator.cookie_name)
    return response.text, cookie.value if cookie else None


async def test_an_account_token_is_checked_on_every_request_and_sets_no_cookie(
    config, registry, caplog
):
    authenticator = config["HubTokenAuthenticator"]()
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    user, cookie = await signed_in(authenticator, Request(bearer))
    assert (user, cookie) == ("account:demo-workload", None)
    assert authenticator.cache == {}
    cookies = {authenticator.cookie_name: "cookie-0"}
    with caplog.at_level(logging.WARNING):
        for refused in (
            Request({**bearer, "X-Forwarded-For": "128.211.160.5"}, cookies=cookies),
            Request(bearer, remote="128.211.160.5", cookies=cookies),
            Request({}, remote="128.211.160.5", cookies=cookies),
        ):
            with pytest.raises(Unauthorized):
                await signed_in(authenticator, refused)
        mount_configmap(registry, "2", {})
        with pytest.raises(Unauthorized):
            await signed_in(authenticator, Request(bearer))
    assert "demo-workload refused: reason=forwarded" in caplog.text
    assert "demo-workload refused: reason=outside-pod-network" in caplog.text
    assert "reason=unknown-token" in caplog.text


async def test_a_users_cookie_lapses_once_an_account_takes_their_name(
    config, tmp_path, caplog
):
    config["ACCOUNTS_DIR"] = str(tmp_path)
    authenticator = config["HubTokenAuthenticator"]()
    users = {}
    for name in ("demo-workload", "someone"):
        user, cookie = await signed_in(
            authenticator, Request({"Authorization": basic(name)})
        )
        assert user == name
        users[name] = {authenticator.cookie_name: cookie}
    declare(tmp_path, **{"demo-workload.yaml": WORKLOAD})
    with caplog.at_level(logging.WARNING), pytest.raises(Unauthorized):
        await signed_in(
            authenticator,
            Request(
                {"Authorization": basic("demo-workload")},
                cookies=users["demo-workload"],
            ),
        )
    assert "Basic user refused: reason=account-name account=demo-workload" in (
        caplog.text
    )
    assert await signed_in(authenticator, Request({}, cookies=users["someone"])) == (
        "someone",
        None,
    )


@pytest.fixture
def chart_config(config):
    """The scheduler's and worker's config as the chart sets it from values.yaml."""
    backend = yaml.safe_load(VALUES.read_text())["gateway"]["backend"]
    shared = config["c"].KubeClusterConfig
    for role in ("scheduler", "worker"):
        shared[f"{role}_extra_pod_config"] = backend[role]["extraPodConfig"]
        shared[f"{role}_extra_container_config"] = backend[role]["extraContainerConfig"]
    return copy.deepcopy(dict(shared))


async def account_cluster(config, options):
    handler, user, threads = await cluster_handler(
        config, "account:demo-workload", None
    )
    assert threads == []
    return handler(options, user)


async def test_an_accounts_cluster_runs_as_it_on_cvmfs_and_its_own_volume(
    config, registry, chart_config
):
    options = request_options(
        pixi_project="/demo-workload/run/jfc", USER="someone", JUPYTERHUB_USER="someone"
    )
    cluster = await account_cluster(config, options)

    assert cluster["scheduler_cmd"][0] == f"{WORKLOAD_ENV}/bin/dask"
    assert cluster["environment"]["PATH"].startswith(f"{WORKLOAD_ENV}/bin/:")
    for role in ("scheduler", "worker"):
        pod = cluster[f"{role}_extra_pod_config"]
        chart_pod = chart_config[f"{role}_extra_pod_config"]
        assert pod["securityContext"] == {"runAsUser": 1234, "runAsGroup": 1235}
        assert {
            k: v for k, v in pod.items() if k not in ("volumes", "securityContext")
        } == {k: v for k, v in chart_pod.items() if k != "volumes"}
        volumes = {v["name"]: v for v in pod["volumes"]}
        assert volumes == {
            "cvmfs": {"name": "cvmfs", "persistentVolumeClaim": {"claimName": "cvmfs"}},
            "demo-workload": {
                "name": "demo-workload",
                "persistentVolumeClaim": {"claimName": "demo-workload"},
            },
        }
        container = cluster[f"{role}_extra_container_config"]
        mounts = {m["name"]: m for m in container["volumeMounts"]}
        assert mounts.keys() == {"cvmfs", "demo-workload"}
        assert mounts["cvmfs"]["mountPath"] == "/cvmfs"
        assert mounts["demo-workload"] == {
            "name": "demo-workload",
            "mountPath": "/demo-workload",
        }
        chart_container = chart_config[f"{role}_extra_container_config"]
        assert container.get("command") == chart_container.get("command")
        assert container["env"] == [
            e
            for e in chart_container["env"]
            if not e["name"].startswith("DASK_DISTRIBUTED__WORKER__PRELOAD")
        ]
        labels = cluster[f"{role}_extra_pod_labels"]
        assert labels == {
            "user": "demo-workload",
            "app.kubernetes.io/part-of": "demo-workload",
        }
        assert all(len(v) <= 63 and LABEL_VALUE.fullmatch(v) for v in labels.values())
    assert not any(
        e["name"].startswith("DASK_DISTRIBUTED__WORKER__PRELOAD")
        for e in cluster["worker_extra_container_config"]["env"]
    )
    assert dict(config["c"].KubeClusterConfig) == chart_config


async def test_monitoring_and_logs_see_an_account_as_the_user_of_its_name(
    config, registry, chart_config
):
    """Alloy and Prometheus take a Dask pod's user from its `user` label."""
    options = request_options(
        pixi_project="/demo-workload/run/jfc",
        USER="someone",
        LOGNAME="someone",
        JUPYTERHUB_USER="someone",
    )
    cluster = await account_cluster(config, options)
    for role in ("scheduler", "worker"):
        assert cluster[f"{role}_extra_pod_labels"]["user"] == "demo-workload"
    for variable in ("USER", "LOGNAME", "NB_USER", "JUPYTERHUB_USER"):
        assert cluster["environment"][variable] == "demo-workload"
    assert "account:" not in json.dumps(cluster)
    assert "someone" not in json.dumps(cluster)


async def test_an_account_no_longer_declared_gets_no_cluster(config, registry):
    handler, user, _ = await cluster_handler(config, "account:gone-workload", None)
    with pytest.raises(ValueError, match="not declared"):
        handler(request_options(pixi_project="/gone-workload/run"), user)


@pytest.mark.parametrize(
    "conda_env, project, pixi_env",
    [
        ("/work/pixi/global/.pixi/envs/default", "", "default"),
        ("", "/depot/cms/users/someone/proj", "default"),
        (
            "",
            "/demo-workload/run/jfc",
            "../../../../../work/pixi/global/.pixi/envs/default",
        ),
        ("", "/demo-workload/run/jfc", "/work/pixi/global/.pixi/envs/default"),
        ("/demo-workload/../work/env", "", "default"),
        ("", "demo-workload/run/jfc", "default"),
        ("demo-workload/env", "", "default"),
        ("", "/demo-workload-old/run/jfc", "default"),
        ("/other-workload/env", "", "default"),
        ("/eos/purdue/store/env", "", "default"),
    ],
)
async def test_an_account_runs_only_an_environment_its_pods_mount(
    config, registry, conda_env, project, pixi_env
):
    options = request_options(conda_env, project)
    options.pixi_env = pixi_env
    with pytest.raises(ValueError, match="not on the storage the cluster mounts"):
        await account_cluster(config, options)


@pytest.mark.parametrize(
    "conda_env, project, error",
    [
        ("/demo-workload/env", "/demo-workload/run", "mutually exclusive"),
        ("", "", "must be specified"),
    ],
)
async def test_an_account_names_one_environment(
    config, registry, conda_env, project, error
):
    with pytest.raises(ValueError, match=error):
        await account_cluster(config, request_options(conda_env, project))


async def test_an_accounts_environment_on_cvmfs_needs_prometheus_client(
    config, registry
):
    with pytest.raises(ValueError, match="prometheus_client is not installed"):
        await account_cluster(config, request_options("/cvmfs/none.invalid/env"))


@pytest.mark.parametrize("username", ["jovyan", "someone-cern"])
def test_a_user_naming_an_accounts_volume_gets_the_prometheus_client_check(
    options_handler, username
):
    options = request_options("/demo-workload/env", NB_UID="4001", NB_GID="4002")
    with pytest.raises(ValueError, match="prometheus_client is not installed"):
        options_handler(options, types.SimpleNamespace(name=username))


def test_a_users_cluster_has_no_account_storage_or_labels(
    config, chart_config, conda_env
):
    cluster = config["options_handler"](
        request_options(conda_env), types.SimpleNamespace(name="jovyan")
    )
    assert cluster.keys() == {
        "worker_cores",
        "worker_cores_limit",
        "worker_memory",
        "worker_memory_limit",
        "environment",
        "scheduler_cmd",
        "worker_cmd",
        "scheduler_extra_pod_config",
        "worker_extra_pod_config",
        "scheduler_extra_pod_labels",
        "worker_extra_pod_labels",
    }
    for role in ("scheduler", "worker"):
        assert cluster[f"{role}_extra_pod_config"] == {
            **chart_config[f"{role}_extra_pod_config"],
            "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
        }
        assert cluster[f"{role}_extra_pod_labels"] == {"user": "jovyan"}
    assert (
        not {"USER", "LOGNAME", "NB_USER", "JUPYTERHUB_USER"}
        & cluster["environment"].keys()
    )


def test_the_registry_follows_the_configmap(config, registry):
    accounts = config["accounts"]
    other = accounts()["other-workload"]
    assert accounts().keys() == {"demo-workload", "other-workload"}
    replaced = account(token="new-token")
    mount_configmap(
        registry,
        "2",
        {
            "demo-workload.yaml": yaml.safe_dump(replaced),
            "other-workload.yaml": yaml.safe_dump(other),
        },
    )
    assert accounts() == {"demo-workload": replaced, "other-workload": other}
    assert config["token_account"](TOKEN) is None
    assert config["token_account"]("new-token") == replaced
    mount_configmap(registry, "3", {"other-workload.yaml": yaml.safe_dump(other)})
    assert accounts() == {"other-workload": other}
    assert config["token_account"]("new-token") is None


def declare(directory, **files):
    for name, declared in files.items():
        (directory / name).write_text(yaml.safe_dump(declared))


@pytest.mark.parametrize("declared, reason", REFUSED_ACCOUNTS)
def test_a_declaration_that_breaks_a_rule_is_refused_and_logged(
    config, tmp_path, caplog, declared, reason
):
    config["ACCOUNTS_DIR"] = str(tmp_path)
    declare(tmp_path, **{"kept.yaml": WORKLOAD, "refused.yaml": declared})
    with caplog.at_level(logging.WARNING):
        assert config["accounts"]() == {"demo-workload": WORKLOAD}
    assert f"refused.yaml refused: reason={reason}" in caplog.text
    assert WORKLOAD["tokenSha256"] not in caplog.text


def test_the_longest_name_is_accepted(config, tmp_path):
    config["ACCOUNTS_DIR"] = str(tmp_path)
    longest = account("a" * config["ACCOUNT_NAME_MAX"])
    declare(tmp_path, **{"longest.yaml": longest})
    assert list(config["accounts"]()) == [longest["name"]]


def test_an_unparsable_declaration_is_refused(config, tmp_path, caplog):
    config["ACCOUNTS_DIR"] = str(tmp_path)
    (tmp_path / "broken.yaml").write_text(
        f"name: [demo\ntokenSha256: {WORKLOAD['tokenSha256']}\n"
    )
    with caplog.at_level(logging.WARNING):
        assert config["accounts"]() == {}
    assert "broken.yaml refused: reason=unreadable" in caplog.text
    assert WORKLOAD["tokenSha256"] not in caplog.text


@pytest.mark.parametrize(
    "second",
    [account(token="second-token", uid=1500), account("second-workload")],
)
def test_declarations_sharing_a_name_or_a_hash_are_all_refused(
    config, tmp_path, caplog, second
):
    config["ACCOUNTS_DIR"] = str(tmp_path)
    other = account("other-workload", "other-token")
    declare(tmp_path, **{"a.yaml": WORKLOAD, "b.yaml": second, "c.yaml": other})
    with caplog.at_level(logging.WARNING):
        assert config["accounts"]() == {"other-workload": other}
    assert "reason=duplicate" in caplog.text


def test_committed_declarations_keep_the_rules(config):
    declared = sorted(REPO.glob("apps/*/account.yaml"))
    assert declared
    for path in declared:
        config["checked_account"](yaml.safe_load(path.read_text()))


def test_the_gateway_mounts_the_registry_it_reads(config):
    """kubelet never updates a subPath mount."""
    release = yaml.safe_load(RELEASE.read_text())
    (patch,) = [
        p
        for p in release["spec"]["postRenderers"][0]["kustomize"]["patches"]
        if p["target"] == {"kind": "Deployment", "name": "api-.*"}
    ]
    ops = yaml.safe_load(patch["patch"])
    volumes = [o["value"] for o in ops if o["path"] == "/spec/template/spec/volumes/-"]
    mounts = [
        o["value"]
        for o in ops
        if o["path"] == "/spec/template/spec/containers/0/volumeMounts/-"
    ]
    (volume,) = [
        v for v in volumes if v.get("configMap", {}).get("name") == "af-accounts"
    ]
    assert volume["configMap"]["optional"] is True
    (mount,) = [m for m in mounts if m["name"] == volume["name"]]
    assert "subPath" not in mount
    assert mount["mountPath"] == config["ACCOUNTS_DIR"]


def test_both_gateways_keep_the_same_rules(config, monkeypatch):
    # gpu_queries.py is in the Ray gateway's ConfigMap, beside gateway.py.
    monkeypatch.syspath_prepend(
        REPO / "apps" / "jupyterhub" / "jupyterhub" / "extraFiles"
    )
    ray = load_script(RAY_GATEWAY, "ray_train_gateway_accounts")
    for rule in (
        "RESERVED_NAMES",
        "RESERVED_PREFIXES",
        "ACCOUNT_IDS",
        "ACCOUNT_NAME_MAX",
    ):
        assert config[rule] == getattr(ray, rule), rule
    assert config["RESERVED_SUFFIXES"] == ray.EXTERNAL_SUFFIXES
    for pattern in ("ACCOUNT_NAME", "TOKEN_SHA256"):
        assert config[pattern].pattern == getattr(ray, pattern).pattern, pattern
    assert config["ACCOUNT_USER"].endswith(":")


def test_clusters_report_to_the_gateway_inside_the_cluster(config):
    """The chart points schedulers at the gateway's Service; an override here
    would route their heartbeats elsewhere."""
    assert "api_url" not in config["c"].get("KubeBackend", {})


def test_lookup_reads_the_dn_at_base_scope(code):
    assert 'search_base = "uid={0},{1}".format(username, baseDN)' in code
    assert "search_scope = BASE" in code


def test_lookup_reports_a_miss_without_searching(code):
    """A miss raises: no search that could match another account, no
    IndexError on entries[0]."""
    assert "SUBTREE" not in code
    assert 'raise ValueError("no LDAP entry for " + username)' in code
    assert "[u'entries'][0]" not in code


def test_lookup_targets_geddes_auth(code):
    assert 'url = "geddes-auth.rcac.purdue.edu"' in code
    assert f'baseDN = "{BASE_DN}"' in code
