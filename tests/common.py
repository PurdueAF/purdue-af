"""Plumbing shared by the test suites."""

import hashlib
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_script(path, module_name):
    """Import a script with a non-importable filename (dashes)."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


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
