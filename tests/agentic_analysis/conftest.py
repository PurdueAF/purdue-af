import types

import af_dask
import pytest


@pytest.fixture
def x509(tmp_path, monkeypatch):
    """The grid proxy a task pod holds, after `gateway`, which clears it."""
    proxy = tmp_path / "x509up"
    proxy.write_bytes(b"proxy")
    monkeypatch.setenv("X509_USER_PROXY", str(proxy))
    return proxy


@pytest.fixture
def inline_thread(monkeypatch):
    """The Dask helper's renewal runs where it starts, to its end."""
    monkeypatch.setattr(
        af_dask.threading,
        "Thread",
        lambda target, daemon: types.SimpleNamespace(start=target),
    )
