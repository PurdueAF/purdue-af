"""Tests for promql_scope.py: which series a caller's query may select. The
matchers are checked the way Prometheus applies them: a label a series lacks
reads as the empty string, and a regex must match the whole value."""

import itertools
import json
import re
import sys

import promql_scope
import pytest
import respx
from errors import UpstreamError, UserError
from httpx import AsyncClient, Response
from promql_scope import Owned, matchers, resolve, scope, without_prefixes

SERIES = f"{promql_scope.PROMETHEUS_URL}/api/v1/series"
ALICE = Owned(
    "alice",
    pods={"purdue-af-7"},
    dask_clusters={"0a1b2c"},
    claims={"claim-alice"},
    volumes={"volume-alice"},
)


def visible(series, owned):
    for m in matchers(owned):
        assert m["type"] == "=~"
        if not re.fullmatch(m["value"], series.get(m["name"], "")):
            return False
    return True


@pytest.mark.parametrize(
    "series",
    [
        {"__name__": "up", "job": "loki", "pod": "loki-0", "namespace": "cms"},
        {"__name__": "node_load1", "instance": "paf-a00"},
        {"__name__": "kube_pod_info", "pod": "hub-6f7c-x", "namespace": "cms"},
        {"__name__": "kube_configmap_info", "configmap": "dask-gateway-config"},
        {
            "__name__": "kube_pod_info",
            "pod": "ray-train-gateway-5d-q",
            "namespace": "cms",
        },
        {"__name__": "flux", "name": "purdue-af-flux"},
        {"__name__": "loki_ingester_streams", "user": "fake"},
        {"__name__": "x", "persistentvolumeclaim": "af-shared-storage"},
        {"__name__": "x", "volume": "volume"},
        # Alice's own.
        {"__name__": "af_home_dir_util", "username": "alice", "pod": "purdue-af-7"},
        {"__name__": "c", "pod": "dask-worker-0a1b2c-17", "namespace": "cms"},
        {"__name__": "c", "service": "dask-0a1b2c"},
        {"__name__": "c", "job": "dask-0a1b2c"},
        {
            "__name__": "k",
            "persistentvolumeclaim": "claim-alice",
            "volume": "volume-alice",
        },
    ],
)
def test_what_alice_sees(series):
    assert visible(series, ALICE)


@pytest.mark.parametrize(
    "series",
    [
        {"__name__": "af_home_dir_util", "username": "bob"},
        {"__name__": "dask_scheduler_tasks", "user": "bob"},
        {"__name__": "c", "pod": "purdue-af-12", "namespace": "cms"},
        {"__name__": "c", "pod": "purdue-af-70"},
        {"__name__": "c", "pod": "dask-worker-ffff-1"},
        {"__name__": "c", "pod": "dask-scheduler-0a1b2c3"},
        {"__name__": "c", "secret": "dask-credentials-ffff"},
        {"__name__": "c", "endpoint": "dask-ffff"},
        {"__name__": "c", "owner_name": "ray-job-1dc49bb9e8d9fecb"},
        {"__name__": "c", "owner": "ray-train-bob"},
        {"__name__": "c", "name": "claim-bob"},
        {"__name__": "c", "volume": "volume-bob"},
        {"__name__": "c", "pod": "hub-1", "namespace": "kubeflow"},
        {"__name__": "c", "claim_namespace": "default"},
        {"__name__": "c", "service_namespace": "istio-system"},
    ],
)
def test_what_alice_does_not_see(series):
    assert not visible(series, ALICE)


def test_someone_who_owns_nothing_sees_no_persons_series():
    assert not visible({"__name__": "x", "username": "alice"}, Owned(""))
    assert visible({"__name__": "x", "pod": "hub-1"}, Owned(""))


def test_a_username_is_matched_literally():
    dotted = Owned("a.b")
    assert visible({"__name__": "x", "username": "a.b"}, dotted)
    assert not visible({"__name__": "x", "username": "axb"}, dotted)


def test_without_prefixes_matches_exactly_the_strings_with_none():
    prefixes = ("ab", "abc-", "b", "c-d")
    regex = re.compile(without_prefixes(prefixes))
    for n in range(6):
        for chars in itertools.product("abc-d", repeat=n):
            text = "".join(chars)
            expected = not any(text.startswith(p) for p in prefixes)
            assert bool(regex.fullmatch(text)) == expected, text


@respx.mock
async def test_resolve_finds_the_callers_pods_clusters_and_volumes():
    def answer(request):
        match = request.url.params.get_list("match[]")
        if any("username" in m for m in match):
            return Response(
                200,
                json={
                    "data": [
                        {"__name__": "a", "username": "alice", "pod": "purdue-af-7"},
                        {
                            "__name__": "b",
                            "user": "alice",
                            "pod": "dask-scheduler-0a1b2c",
                        },
                        {"__name__": "c", "user": "alice", "pod": "loki-0"},
                    ]
                },
            )
        assert match == [
            promql_scope.OWNED_PVC_QUERY
            + '{namespace="cms",pod=~"purdue\\\\-af\\\\-7"}'
        ]
        return Response(
            200,
            json={
                "data": [
                    {"persistentvolumeclaim": "claim-alice", "volume": "volume-alice"}
                ]
            },
        )

    respx.get(SERIES).mock(side_effect=answer)
    async with AsyncClient() as client:
        owned = await resolve(client, "alice", 0, 1)
    assert owned == ALICE


@respx.mock
async def test_resolve_refuses_to_guess_when_prometheus_is_down():
    respx.get(SERIES).mock(return_value=Response(503, json={}))
    async with AsyncClient() as client:
        with pytest.raises(UpstreamError):
            await resolve(client, "alice", 0, 1)


@pytest.fixture
def fake_binary(tmp_path, monkeypatch):
    """A promql-scope that answers with what the test writes to `answer`."""
    answer = tmp_path / "answer.json"
    seen = tmp_path / "seen.json"
    script = tmp_path / "promql-scope"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(seen)!r}, 'w').write(sys.stdin.read())\n"
        f"print(open({str(answer)!r}).read())\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(promql_scope, "SCOPE_BINARY", str(script))
    return answer, seen


async def test_scope_sends_the_query_and_the_callers_matchers(fake_binary):
    answer, seen = fake_binary
    answer.write_text(json.dumps({"query": "scoped"}))
    assert await scope("up", ALICE) == "scoped"
    sent = json.loads(seen.read_text())
    assert sent == {"query": "up", "matchers": matchers(ALICE)}


async def test_a_query_that_does_not_parse_is_the_callers_error(fake_binary):
    answer, _ = fake_binary
    answer.write_text(json.dumps({"error": "unexpected end of input"}))
    with pytest.raises(UserError, match="unexpected end of input"):
        await scope("sum(", ALICE)


async def test_a_missing_binary_is_not_the_callers_error(monkeypatch):
    monkeypatch.setattr(promql_scope, "SCOPE_BINARY", "/nonexistent/promql-scope")
    with pytest.raises(UpstreamError):
        await scope("up", ALICE)
