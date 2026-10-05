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


def test_env_names_kubernetes_rejects_are_dropped(options_handler):
    options = types.SimpleNamespace(
        env={
            "PATH": "/usr/bin",
            "X509_USER_PROXY": "/work/users/someone/x509up",
            "my.env-name": "kept",
            "BASH_FUNC_which%%": "() {  ( alias; eval ${which_declare} ) | /usr/bin/which $@\n}",
            "1_LEADING_DIGIT": "dropped",
        },
        conda_env="/opt/env",
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
