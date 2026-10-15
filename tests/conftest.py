"""Root conftest: anchors tests/ on sys.path so suites can import common.py,
and splits the suite into shards for CI's parallel check-unit jobs."""

import zlib

import pytest


def pytest_addoption(parser):
    parser.addoption("--shard", metavar="K/N", help="run only shard K (1-based) of N")


def pytest_collection_modifyitems(config, items):
    spec = config.getoption("--shard")
    if not spec:
        return
    k, n = (int(part) for part in spec.split("/"))
    if not 1 <= k <= n:
        raise pytest.UsageError(f"--shard {spec}: K must be between 1 and N")
    keep, drop = [], []
    for item in items:
        # crc32, not hash(): every xdist worker must deselect the same tests.
        shard = zlib.crc32(item.nodeid.encode()) % n + 1
        (keep if shard == k else drop).append(item)
    items[:] = keep
    config.hook.pytest_deselected(items=drop)
