"""Test helpers for the JupyterHub extraFiles config snippets.

The snippets are not importable modules: the hub `exec`s them with a config
object `c` in scope. We replicate that here (ConfigSink), so the files under
test are byte-identical to what runs in production.
"""

import logging
import types

from common import REPO, ConfigSink
from kubernetes_asyncio.client import V1ConfigMap, V1ObjectMeta
from kubernetes_asyncio.client.rest import ApiException

EXTRA_FILES = REPO / "apps" / "jupyterhub" / "jupyterhub" / "extraFiles"


def load_snippet(filename, monkeypatch, namespace="cms", extra_globals=None):
    """Exec an extraFiles snippet the way JupyterHub does; return its globals."""
    monkeypatch.setenv("POD_NAMESPACE", namespace)
    # gpu-availability.py imports the shared gpu_queries module from the
    # snippet directory; in the source tree that is extraFiles itself.
    monkeypatch.setenv("JUPYTERHUB_CONFIG_D", str(EXTRA_FILES))
    ns = {"c": ConfigSink()}
    if extra_globals:
        ns.update(extra_globals)
    code = (EXTRA_FILES / filename).read_text()
    exec(compile(code, str(EXTRA_FILES / filename), "exec"), ns)
    return ns


class FakeConfigMaps:
    """One ConfigMap as kubernetes_asyncio's CoreV1Api serves it: absent until
    created, and a replace must carry the resourceVersion of what it read."""

    def __init__(self, data=None):
        self.data = None if data is None else dict(data)
        self.version = 0
        self.calls = []
        # The _request_timeout of every call
        self.timeouts = []
        # HTTP status every read fails with
        self.fail_read = None
        # Applied to the data, as another writer, just before the next replace is refused
        self.on_conflict = None
        self.always_conflict = False
        # What another writer creates just before our create
        self.race_create = None

    def _served(self, name):
        return V1ConfigMap(
            metadata=V1ObjectMeta(name=name, resource_version=str(self.version)),
            data=dict(self.data) or None,
        )

    async def read_namespaced_config_map(self, name, namespace, **kwargs):
        self.calls.append(("read", name, namespace))
        self.timeouts.append(kwargs.get("_request_timeout"))
        if self.fail_read:
            raise ApiException(status=self.fail_read)
        if self.data is None:
            raise ApiException(status=404)
        return self._served(name)

    async def create_namespaced_config_map(self, namespace, body, **kwargs):
        self.calls.append(("create", body.metadata.name, namespace))
        self.timeouts.append(kwargs.get("_request_timeout"))
        if self.race_create is not None:
            self.data, self.race_create = dict(self.race_create), None
            self.version += 1
        if self.data is not None:
            raise ApiException(status=409)
        self.data = dict(body.data)
        self.version += 1
        return self._served(body.metadata.name)

    async def replace_namespaced_config_map(self, name, namespace, body, **kwargs):
        self.calls.append(("replace", name, namespace))
        self.timeouts.append(kwargs.get("_request_timeout"))
        if self.on_conflict is not None:
            mutate, self.on_conflict = self.on_conflict, None
            mutate(self.data)
            self.version += 1
            raise ApiException(status=409)
        if self.always_conflict or body.metadata.resource_version != str(self.version):
            raise ApiException(status=409)
        self.data = dict(body.data)
        self.version += 1
        return self._served(name)


class FakeDB:
    """The hub ORM session, answering the users query with (id, name) rows."""

    def __init__(self, users):
        self.users = list(users)

    def query(self, *columns):
        return types.SimpleNamespace(all=lambda: list(self.users))


class FakeSpawner:
    """Minimal KubeSpawner stand-in for hook tests.

    `users` are the hub's (id, name) rows the ledger is seeded from; `ledger`
    is the ConfigMap's data, or None when it does not exist yet.
    """

    def __init__(self, user_id=1, users=(), ledger=None, api=None):
        self.environment = {}
        self.userdata = None
        self.namespace = "cms"
        self.k8s_api_request_timeout = 3
        self.log = logging.getLogger("fake-spawner")
        self.api = api or FakeConfigMaps(ledger)
        self.user = types.SimpleNamespace(id=user_id, db=FakeDB(users))
