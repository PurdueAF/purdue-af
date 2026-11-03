"""Plumbing shared by the test suites."""

import hashlib
import importlib.util
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
EXPERIMENTAL = REPO / "flux" / "experimental" / "kustomization.yaml"
CORE = REPO / "flux" / "core-production" / "kustomization.yaml"


def load_yaml(path):
    """The single document of a YAML file."""
    return yaml.safe_load(path.read_text())


def generator(root, name):
    """The configMapGenerator entry `name` of a parsed Flux root."""
    (entry,) = [g for g in root["configMapGenerator"] if g["name"] == name]
    return entry


def load_script(path, module_name):
    """Import a script with a non-importable filename (dashes)."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_all(path):
    """The documents of a YAML file."""
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


HUB_VALUES = REPO / "apps" / "jupyterhub" / "jupyterhub" / "values.yaml"
EXTRA_FILES = HUB_VALUES.with_name("extraFiles")


def hub_values():
    """values.yaml as the hub reads it: Helm renders every map key as a string."""

    def rendered(value):
        if isinstance(value, dict):
            return {str(k): rendered(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rendered(v) for v in value]
        return value

    return rendered(yaml.safe_load(HUB_VALUES.read_text()))


def mount_configmap(volume, version, files):
    """Swap `files` into `volume` as kubelet does: a new directory, `..data`
    relinked to it in one rename, each file a link through `..data`."""
    data = volume / f"..{version}"
    data.mkdir()
    for name, text in files.items():
        (data / name).write_text(text)
        if not (volume / name).is_symlink():
            (volume / name).symlink_to(f"..data/{name}")
    (volume / "..data_tmp").symlink_to(data.name)
    (volume / "..data_tmp").replace(volume / "..data")


def sample(value, **labels):
    """A sample of a Prometheus answer."""
    return {"metric": labels, "value": [0, str(value)]}


class ConfigSink(dict):
    """Accepts both attribute traversal (c.ServerApp.ip = 1) and dict ops
    (c.KubeSpawner.environment.setdefault(...)), like a traitlets config."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        child = self.get(name)
        if child is None:
            child = ConfigSink()
            self[name] = child
        return child

    def __setattr__(self, name, value):
        self[name] = value


def account(name="demo-workload", token="demo-token", uid=1234, gid=1235):
    """A workload account's declaration, its token hashed as account.yaml holds it."""
    return {
        "name": name,
        "uid": uid,
        "gid": gid,
        "tokenSha256": hashlib.sha256(token.encode()).hexdigest(),
    }


# Declarations both gateways refuse, and the reason each logs.
REFUSED_ACCOUNTS = [
    *(
        ({**account(), "name": name}, "name")
        for name in ("Demo", "demo_workload", "-demo", "demo-", "demo\n", "a" * 44, 7)
    ),
    *(
        ({**account(), "name": name}, "reserved-name")
        for name in (
            # Top-level directories of the clusters' pods.
            *"bin boot dev etc home lib lib32 lib64 libx32 media mnt opt proc root".split(),
            *"run sbin srv sys tmp usr var".split(),
            # The AF's storage roots, PVCs and image user, and the clusters' volumes.
            *"work depot eos cvmfs af-shared-storage hub-db-dir jovyan".split(),
            "dask-credentials",
            "log-volume",
            "claim-someone",
            "claim-demo-workload",
            "someone-cern",
            "someone-fnal",
        )
    ),
    *(
        ({**account(), "uid": uid}, "uid")
        for uid in (999, 2000, 0, "1100", True, 1100.0)
    ),
    *(({**account(), "gid": gid}, "gid") for gid in (100, 65534, None)),
    *(
        ({**account(), "tokenSha256": digest}, "token-hash")
        for digest in ("A" * 64, "a" * 63, "g" * 64, "demo-token", None)
    ),
    ({**account(), "role": "admin"}, "keys"),
    ({k: v for k, v in account().items() if k != "gid"}, "keys"),
    (["demo-workload"], "keys"),
    (None, "keys"),
]
