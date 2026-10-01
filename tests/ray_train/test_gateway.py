"""Tests for apps/ray-train/gateway.py against fake JupyterHub, Kubernetes and Ray heads.

What the isolation rests on: a call is resolved to a user only by the Hub, it
reaches that user's cluster and no other, the cluster runs as the user's LDAP
account, and a head only ever sees its own derived token, never a session's.
"""

import asyncio
import copy
import json
import socket
import ssl
import sys
import time
import types
from types import SimpleNamespace

import grpc
import pytest
import yaml
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from common import REPO, load_script

gw = load_script(REPO / "apps" / "ray-train" / "gateway.py", "ray_train_gateway")

TEMPLATE = REPO / "apps" / "ray-train" / "raycluster.yaml"
SERVICE_TOKEN = "service-token"
# session token -> (JupyterHub name, AF id of the running session, or None)
SESSIONS = {
    "session-a": ("user-a", 7),
    "session-b": ("user-b-cern", 42),
    "session-idle": ("user-c-fnal", None),
}
# Users the fake Hub was asked about
LOOKUPS = []
LDAP = {"user-a": (5001, 500), "paf0042": (6042, 600)}
ENV_A = "/work/users/user-a/proj/.pixi/envs/default"
ENV_B = "/depot/cms/users/user-a/other"
# Ray Client methods: a unary call, a stream, and one the fake head refuses.
PING = "/ray.rpc.RayletDriver/ClusterInfo"
DATAPATH = "/ray.rpc.RayletDataStreamer/Datapath"
FAILING = "/ray.rpc.RayletDriver/GetObject"
LOGS = "/ray.rpc.RayletLogStreamer/Logstream"


def free_port():
    """An address nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def hub_app():
    async def whoami(request):
        _, _, token = request.headers.get("Authorization", "").partition(" ")
        if token == "hub-down":
            return web.json_response({}, status=500)
        if token not in SESSIONS:
            return web.json_response({}, status=403)
        return web.json_response({"name": SESSIONS[token][0]})

    async def user(request):
        if request.headers.get("Authorization") != f"token {SERVICE_TOKEN}":
            return web.json_response({}, status=403)
        name = request.match_info["name"]
        LOOKUPS.append(name)
        (af_id,) = [i for n, i in SESSIONS.values() if n == name]
        servers = (
            {} if af_id is None else {"": {"state": {"pod_name": f"purdue-af-{af_id}"}}}
        )
        return web.json_response({"name": name, "servers": servers})

    app = web.Application()
    app.router.add_get("/hub/api/user", whoami)
    app.router.add_get("/hub/api/users/{name}", user)
    return app


class FakeKube:
    def __init__(self):
        self.clusters = {}
        self.secrets = {}
        self.fail_get = False
        self.fail_list = False
        self.reject_create = False
        self.reject_secret = False
        # Clusters another call is creating, absent to a GET and present to a POST:
        # name -> the environment that call asks for, or None if it deletes the cluster again.
        self.racing = {}
        # Clusters being deleted: name -> the GETs that still find them.
        self.linger = {}
        # uid -> name of each cluster whose head pod is up, and the most there ever were.
        self.pods = {}
        self.most_pods = 0
        self.created = 0

    def app(self):
        clusters = "/apis/ray.io/v1/namespaces/cms/rayclusters"

        def stored(body):
            self.created += 1
            name = body["metadata"]["name"]
            body["metadata"]["uid"] = f"uid-{name}-{self.created}"
            self.clusters[name] = body
            self.pods[body["metadata"]["uid"]] = name
            self.most_pods = max(self.most_pods, len(self.pods))

        def removed(name):
            self.pods.pop(self.clusters.pop(name)["metadata"]["uid"])

        async def create(request):
            body = await request.json()
            name = body["metadata"]["name"]
            if self.reject_create:
                return web.json_response({"message": "invalid spec"}, status=422)
            if name in self.racing:
                other_env = self.racing.pop(name)
                if other_env is not None:
                    body["metadata"]["annotations"][gw.ENV_ANNOTATION] = other_env
                    stored(body)
                return web.json_response({"message": "exists"}, status=409)
            if name in self.clusters:
                return web.json_response({"message": "exists"}, status=409)
            stored(body)
            return web.json_response(body, status=201)

        async def listing(request):
            if self.fail_list:
                return web.json_response({}, status=500)
            return web.json_response({"items": list(self.clusters.values())})

        async def get(request):
            name = request.match_info["name"]
            if self.fail_get:
                return web.json_response({}, status=500)
            if self.linger.get(name) == 0:
                del self.linger[name]
                removed(name)
            elif name in self.linger:
                self.linger[name] -= 1
            if name in self.racing or name not in self.clusters:
                return web.json_response({"message": "not found"}, status=404)
            return web.json_response(self.clusters[name])

        async def delete(request):
            name = request.match_info["name"]
            if name not in self.clusters:
                return web.json_response({}, status=404)
            if (await request.json()).get("propagationPolicy") == "Foreground":
                # The cluster stays, marked, until its pod is gone.
                self.clusters[name]["metadata"]["deletionTimestamp"] = "now"
                self.linger.setdefault(name, 1)
            else:
                # The cluster goes at once, its pod some time later.
                del self.clusters[name]
            return web.json_response({})

        async def secret(request):
            body = await request.json()
            name = body["metadata"]["name"]
            if self.reject_secret:
                return web.json_response({"message": "forbidden"}, status=403)
            if name in self.secrets:
                return web.json_response({"message": "exists"}, status=409)
            self.secrets[name] = body
            return web.json_response(body, status=201)

        app = web.Application()
        app.router.add_post(clusters, create)
        app.router.add_get(clusters, listing)
        app.router.add_get(clusters + "/{name}", get)
        app.router.add_delete(clusters + "/{name}", delete)
        app.router.add_post("/api/v1/namespaces/cms/secrets", secret)
        return app


class FakeHeads:
    """Every user's Ray head: a dashboard at /<cluster>/... that accepts only its
    own token, and one Ray Client server standing in for all of them."""

    def __init__(self):
        self.up = True
        # A head whose Ray predates token authentication serves anyone.
        self.open = False
        self.down_checks = 0
        # cluster -> its running tasks
        self.running = {}
        # (method, metadata) of every call a head's Ray Client server took
        self.calls = []
        # Holds back a log stream's second line.
        self.more_logs = asyncio.Event()

    def dashboards(self):
        @web.middleware
        async def check(request, handler):
            if not self.up:
                raise web.HTTPServiceUnavailable()
            cluster = request.match_info.get("cluster", "")
            token = request.headers.get("Authorization", "")
            expected = f"Bearer {gw.cluster_token(SERVICE_TOKEN, cluster)}"
            if not self.open and token != expected:
                raise web.HTTPUnauthorized()
            return await handler(request)

        async def version(request):
            if self.down_checks:
                self.down_checks -= 1
                raise web.HTTPServiceUnavailable()
            return web.json_response({"version": "4", "ray_version": "2.58.0"})

        async def tasks(request):
            assert (request.query["filter_keys"], request.query["filter_values"]) == (
                "state",
                "RUNNING",
            )
            running = self.running.get(request.match_info["cluster"], [])
            limit = int(request.query["limit"])
            listing = {"total": len(running), "result": running[:limit]}
            return web.json_response({"result": True, "data": {"result": listing}})

        app = web.Application(middlewares=[check])
        app.router.add_get("/{cluster}/api/version", version)
        app.router.add_get("/{cluster}/api/v0/tasks", tasks)
        return app

    def client_server(self):
        calls, more_logs = self.calls, self.more_logs

        class Handler(grpc.GenericRpcHandler):
            def service(self, details):
                method = details.method

                async def answer(requests, context):
                    calls.append((method, dict(context.invocation_metadata())))
                    if method == FAILING:
                        await context.abort(grpc.StatusCode.NOT_FOUND, "no such object")
                    if method == LOGS:
                        yield b"log 1"
                        await more_logs.wait()
                        yield b"log 2"
                        return
                    async for request in requests:
                        yield b"echo:" + request

                return grpc.stream_stream_rpc_method_handler(answer)

        server = grpc.aio.server()
        server.add_generic_rpc_handlers((Handler(),))
        return server


@pytest.fixture
async def env(monkeypatch, tmp_path):
    LOOKUPS.clear()
    kube, heads = FakeKube(), FakeHeads()
    servers = [
        TestServer(hub_app()),
        TestServer(kube.app()),
        TestServer(heads.dashboards()),
    ]
    for server in servers:
        await server.start_server()
    hub, kube_server, dashboards = servers
    client_server = heads.client_server()
    client_port = client_server.add_insecure_port("127.0.0.1:0")
    await client_server.start()
    (tmp_path / "token").write_text("sa-token")
    token_file = tmp_path / "hub-token"
    token_file.write_text(SERVICE_TOKEN + "\n")
    monkeypatch.setattr(gw, "HUB_API", str(hub.make_url("/hub/api")))
    monkeypatch.setattr(gw, "KUBE_API", str(kube_server.make_url("")).rstrip("/"))
    monkeypatch.setattr(gw, "SERVICE_ACCOUNT", tmp_path)
    monkeypatch.setattr(gw, "TEMPLATE", TEMPLATE)
    monkeypatch.setattr(gw, "START_TIMEOUT_S", 5)
    monkeypatch.setattr(gw, "START_POLL_S", 0)
    monkeypatch.setattr(gw, "ldap_ids", lambda account: LDAP[account])
    dashboards_url = str(dashboards.make_url("")).rstrip("/")
    monkeypatch.setattr(gw, "head_url", lambda cluster: f"{dashboards_url}/{cluster}")
    monkeypatch.setattr(
        gw, "client_address", lambda cluster: f"127.0.0.1:{client_port}"
    )

    async with ClientSession() as http, ClientSession() as kube_http:
        gateway = gw.Gateway(http, kube_http, token_file)
        server = grpc.aio.server(options=gw.GRPC_OPTIONS)
        server.add_generic_rpc_handlers((gw.Relay(gateway),))
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        yield SimpleNamespace(
            channel=channel,
            gateway=gateway,
            kube=kube,
            heads=heads,
            token_file=token_file,
        )
        await channel.close()
        await server.stop(None)
        await gateway.close()
    await client_server.stop(None)
    for server in servers:
        await server.close()


async def call(
    env,
    token="session-a",
    method=PING,
    messages=(b"ping",),
    env_path=None,
    client="client-1",
):
    """A Ray Client call from a session, as the stream of raw messages the gateway sees."""
    metadata = [("client_id", client)]
    if token is not None:
        metadata.append(("authorization", f"Bearer {token}"))
    if env_path is not None:
        metadata.append((gw.ENV_METADATA, env_path))
    responses = env.channel.stream_stream(method)(iter(messages), metadata=metadata)
    return [response async for response in responses]


async def refused(env, **kwargs):
    with pytest.raises(grpc.aio.AioRpcError) as e:
        await call(env, **kwargs)
    return e.value.code()


def head_pod(env, name="ray-train-user-a"):
    return env.kube.clusters[name]["spec"]["headGroupSpec"]["template"]["spec"]


def head_env(env, name="ray-train-user-a"):
    (container,) = head_pod(env, name)["containers"]
    return {v["name"]: v["value"] for v in container["env"]}


def cluster_env(env, name="ray-train-user-a"):
    return env.kube.clusters[name]["metadata"]["annotations"][gw.ENV_ANNOTATION]


def live(env):
    """The clusters not being deleted."""
    return {
        name
        for name, cluster in env.kube.clusters.items()
        if not cluster["metadata"].get("deletionTimestamp")
    }


def idle(env, *clusters):
    long_ago = time.monotonic() - gw.IDLE_TIMEOUT_S - 1
    env.gateway.last_used.update(dict.fromkeys(clusters, long_ago))


async def test_a_call_without_a_token_is_refused(env):
    assert await refused(env, token=None) == grpc.StatusCode.UNAUTHENTICATED
    assert env.kube.clusters == {}


async def test_a_token_the_hub_does_not_know_is_refused(env):
    assert await refused(env, token="stolen") == grpc.StatusCode.UNAUTHENTICATED
    assert env.kube.clusters == {}


async def test_nothing_is_relayed_before_the_hub_registers_the_service(env):
    env.token_file.unlink()
    assert await refused(env) == grpc.StatusCode.UNAVAILABLE
    assert env.kube.clusters == {}


async def test_the_service_token_is_read_when_it_appears(env):
    env.token_file.unlink()
    await refused(env)
    env.token_file.write_text(SERVICE_TOKEN)
    assert await call(env) == [b"echo:ping"]


async def test_connecting_starts_the_users_cluster_as_them(env):
    assert await call(env) == [b"echo:ping"]
    cluster = env.kube.clusters["ray-train-user-a"]
    assert (
        cluster["metadata"]["labels"]["app.kubernetes.io/managed-by"] == gw.MANAGED_BY
    )
    assert cluster["spec"]["authOptions"]["secretName"] == "ray-train-user-a"
    context = head_pod(env)["securityContext"]
    assert (context["runAsUser"], context["runAsGroup"]) == (5001, 500)
    assert 100 in context["supplementalGroups"]
    assert head_env(env)["USER"] == "user-a"
    secret = env.kube.secrets["ray-train-user-a"]
    assert secret["stringData"]["auth_token"] == gw.cluster_token(
        SERVICE_TOKEN, "ray-train-user-a"
    )
    assert secret["metadata"]["ownerReferences"][0]["uid"] == cluster["metadata"]["uid"]


async def test_a_head_sees_its_own_token_and_the_clients_own_metadata(env):
    await call(env, env_path=ENV_A)
    ((method, metadata),) = env.heads.calls
    assert method == PING
    assert metadata["authorization"] == (
        f"Bearer {gw.cluster_token(SERVICE_TOKEN, 'ray-train-user-a')}"
    )
    assert metadata["client_id"] == "client-1"
    assert gw.ENV_METADATA not in metadata
    assert "session-a" not in json.dumps(metadata)


async def test_an_external_user_runs_as_their_pooled_account(env):
    await call(env, token="session-b")
    assert (
        head_pod(env, "ray-train-user-b-cern")["securityContext"]["runAsUser"] == 6042
    )
    assert head_env(env, "ray-train-user-b-cern")["USER"] == "paf0042"


async def test_a_purdue_user_is_not_looked_up_in_the_hub(env):
    await call(env)
    assert LOOKUPS == []
    await call(env, token="session-b")
    assert LOOKUPS == ["user-b-cern"]


async def test_a_user_from_outside_purdue_without_a_running_session_is_told_to_start_one(
    env,
):
    assert (
        await refused(env, token="session-idle") == grpc.StatusCode.FAILED_PRECONDITION
    )
    assert env.kube.clusters == {}


async def test_users_reach_only_their_own_cluster(env):
    await call(env, token="session-a")
    await call(env, token="session-b")
    assert [metadata["authorization"] for _, metadata in env.heads.calls] == [
        f"Bearer {gw.cluster_token(SERVICE_TOKEN, name)}"
        for name in ("ray-train-user-a", "ray-train-user-b-cern")
    ]


async def test_streams_are_relayed_both_ways(env):
    replies = await call(env, method=DATAPATH, messages=(b"a", b"b", b"c"))
    assert replies == [b"echo:a", b"echo:b", b"echo:c"]


async def test_a_heads_error_comes_back_as_it_is(env):
    with pytest.raises(grpc.aio.AioRpcError) as e:
        await call(env, method=FAILING)
    assert (e.value.code(), e.value.details()) == (
        grpc.StatusCode.NOT_FOUND,
        "no such object",
    )


async def test_calls_to_a_started_cluster_skip_the_checks(env):
    await call(env)
    env.kube.fail_get = True
    assert await call(env) == [b"echo:ping"]


async def test_an_unreachable_head_is_checked_again_on_the_next_call(env, monkeypatch):
    port = free_port()
    monkeypatch.setattr(gw, "client_address", lambda cluster: f"127.0.0.1:{port}")
    assert await refused(env) == grpc.StatusCode.UNAVAILABLE
    assert "ray-train-user-a" not in env.gateway.started


async def test_a_client_of_a_removed_cluster_is_told_so_and_starts_nothing(env):
    await call(env)
    idle(env, "ray-train-user-a")
    await env.gateway.reap_idle()
    assert await refused(env) == grpc.StatusCode.NOT_FOUND
    assert live(env) == set()
    assert await call(env, client="client-2") == [b"echo:ping"]
    assert "ray-train-user-a" in env.kube.clusters


async def test_a_removed_clusters_clients_are_forgotten_in_time(env, monkeypatch):
    monkeypatch.setattr(gw, "GONE_S", 0)
    await call(env)
    idle(env, "ray-train-user-a")
    await env.gateway.reap_idle()
    assert await call(env) == [b"echo:ping"]


async def test_a_user_never_holds_two_gpu_pods(env):
    """A replaced or removed cluster's pod is gone before its successor's starts."""
    await call(env, env_path=ENV_A)
    await call(env, env_path=ENV_B)
    idle(env, "ray-train-user-a")
    await env.gateway.reap_idle()
    await call(env, env_path=ENV_A, client="client-2")
    assert env.kube.created == 3
    assert env.kube.most_pods == 1


async def test_a_client_switching_environments_keeps_its_session(env):
    await call(env, env_path=ENV_A)
    await call(env, env_path=ENV_B)
    assert await call(env, env_path=ENV_B) == [b"echo:ping"]


async def test_a_head_that_comes_up_late_is_waited_for(env):
    env.heads.down_checks = 2
    assert await call(env) == [b"echo:ping"]


@pytest.mark.parametrize("head", ["not ready", "unreachable"])
async def test_a_cluster_that_does_not_start_is_reported(env, monkeypatch, head):
    if head == "not ready":
        env.heads.up = False
    else:
        port = free_port()
        monkeypatch.setattr(gw, "head_url", lambda cluster: f"http://127.0.0.1:{port}")
    monkeypatch.setattr(gw, "START_TIMEOUT_S", 0)
    assert await refused(env) == grpc.StatusCode.UNAVAILABLE


@pytest.mark.parametrize(
    "other_env, joined",
    [(gw.DEFAULT_ENV, True), (ENV_A, False), (None, False)],
    ids=["same-environment", "other-environment", "deleted-again"],
)
async def test_a_concurrent_creation_is_joined_only_if_it_runs_the_same_environment(
    env, other_env, joined
):
    env.kube.racing["ray-train-user-a"] = other_env
    if joined:
        assert await call(env) == [b"echo:ping"]
    else:
        assert await refused(env) == grpc.StatusCode.ABORTED


async def test_a_cluster_being_deleted_is_waited_out(env):
    await call(env)
    env.gateway.started.clear()
    env.kube.clusters["ray-train-user-a"]["metadata"]["deletionTimestamp"] = "now"
    env.kube.linger["ray-train-user-a"] = 2
    assert await call(env) == [b"echo:ping"]
    assert "deletionTimestamp" not in env.kube.clusters["ray-train-user-a"]["metadata"]


async def test_a_cluster_that_stays_in_deletion_is_reported(env, monkeypatch):
    await call(env)
    env.gateway.started.clear()
    env.kube.clusters["ray-train-user-a"]["metadata"]["deletionTimestamp"] = "now"
    monkeypatch.setattr(gw, "START_TIMEOUT_S", 0)
    assert await refused(env) == grpc.StatusCode.UNAVAILABLE


async def test_a_named_environment_runs_the_whole_cluster(env):
    await call(env, env_path=ENV_A)
    assert cluster_env(env) == ENV_A
    variables = head_env(env)
    assert variables["PATH"].startswith(f"{ENV_A}/bin:")
    assert variables["CONDA_PREFIX"] == ENV_A


async def test_without_an_environment_the_global_one_runs(env):
    await call(env)
    assert cluster_env(env) == gw.DEFAULT_ENV
    assert head_env(env)["PATH"].startswith(f"{gw.DEFAULT_ENV}/bin:")


@pytest.mark.parametrize(
    "path",
    ["/home/user-a/env", "work/users/user-a/env", "/work/../etc", "/workshop/env"],
)
async def test_an_environment_off_the_clusters_storage_is_refused(env, path):
    assert await refused(env, env_path=path) == grpc.StatusCode.INVALID_ARGUMENT
    assert env.kube.clusters == {}


async def test_switching_environments_replaces_an_idle_cluster(env):
    await call(env, env_path=ENV_A)
    assert await call(env, env_path=ENV_B) == [b"echo:ping"]
    assert cluster_env(env) == ENV_B


async def test_switching_environments_leaves_a_busy_cluster_alone(env):
    await call(env, env_path=ENV_A)
    env.heads.running["ray-train-user-a"] = [{"task_id": "t", "state": "RUNNING"}]
    assert await refused(env, env_path=ENV_B) == grpc.StatusCode.FAILED_PRECONDITION
    assert cluster_env(env) == ENV_A


async def test_a_ray_without_token_authentication_is_refused(env):
    env.heads.open = True
    assert await refused(env, env_path=ENV_A) == grpc.StatusCode.INVALID_ARGUMENT
    assert live(env) == set()


def test_environments_live_on_the_storage_the_cluster_mounts():
    template = yaml.safe_load(TEMPLATE.read_text())
    assert gw.env_roots(template) == ["/work", "/depot/cms", "/eos", "/cvmfs"]
    assert gw.requested_env("", template) == gw.DEFAULT_ENV
    assert gw.requested_env(ENV_A + "/", template) == ENV_A


@pytest.mark.parametrize(
    "failure, token",
    [
        ("fail_get", "session-a"),
        ("reject_create", "session-a"),
        ("reject_secret", "session-a"),
        (None, "hub-down"),
    ],
)
async def test_upstream_failures_are_reported_as_unavailable(env, failure, token):
    if failure:
        setattr(env.kube, failure, True)
    assert await refused(env, token=token) == grpc.StatusCode.UNAVAILABLE


async def test_a_service_token_the_hub_refuses_is_reported(env):
    env.token_file.write_text("not-the-service-token")
    assert await refused(env, token="session-b") == grpc.StatusCode.UNAVAILABLE


async def test_idle_clusters_are_deleted_and_busy_ones_kept(env):
    await call(env, token="session-a")
    await call(env, token="session-b")
    env.heads.running["ray-train-user-a"] = [{"task_id": "t", "state": "RUNNING"}]
    idle(env, "ray-train-user-a", "ray-train-user-b-cern")

    await env.gateway.reap_idle()

    assert live(env) == {"ray-train-user-a"}
    assert set(env.gateway.channels) == {"ray-train-user-a"}


async def test_relayed_traffic_keeps_a_cluster(env):
    await call(env)
    idle(env, "ray-train-user-a")
    await call(env, method=DATAPATH, messages=(b"x",))
    await env.gateway.reap_idle()
    assert "ray-train-user-a" in env.kube.clusters


async def test_logs_from_a_cluster_do_not_keep_it(env):
    await call(env)
    lines = []
    stream = env.channel.stream_stream(LOGS)(
        iter([b"subscribe"]), metadata=[("authorization", "Bearer session-a")]
    )
    async for line in stream:
        lines.append(line)
        idle(env, "ray-train-user-a")
        env.heads.more_logs.set()
    assert lines == [b"log 1", b"log 2"]
    await env.gateway.reap_idle()
    assert live(env) == set()


async def test_a_ready_head_that_does_not_answer_is_kept(env):
    await call(env)
    env.kube.clusters["ray-train-user-a"]["status"] = {
        "conditions": [{"type": "HeadPodReady", "status": "True"}]
    }
    env.heads.up = False
    idle(env, "ray-train-user-a")

    await env.gateway.reap_idle()

    assert "ray-train-user-a" in env.kube.clusters


async def test_an_unreachable_head_that_never_came_up_is_deleted(env, monkeypatch):
    await call(env)
    port = free_port()
    monkeypatch.setattr(gw, "head_url", lambda cluster: f"http://127.0.0.1:{port}")
    idle(env, "ray-train-user-a")
    await env.gateway.reap_idle()
    assert live(env) == set()


async def test_a_cluster_made_under_another_service_token_is_deleted(env):
    await call(env)
    env.token_file.write_text("rotated-service-token")
    idle(env, "ray-train-user-a")
    await env.gateway.reap_idle()
    assert live(env) == set()


async def test_reaping_waits_out_a_failed_listing(env):
    await call(env)
    env.kube.fail_list = True
    idle(env, "ray-train-user-a")
    await env.gateway.reap_idle()
    assert "ray-train-user-a" in env.kube.clusters


async def test_the_reaper_outlives_its_errors(monkeypatch):
    calls = []

    async def failing_reap():
        calls.append(1)
        raise RuntimeError("Kubernetes is down")

    monkeypatch.setattr(gw, "REAP_EVERY_S", 0)
    reaper = asyncio.create_task(
        gw.reap_forever(SimpleNamespace(reap_idle=failing_reap))
    )
    while len(calls) < 2:
        await asyncio.sleep(0)
    reaper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reaper


async def test_the_gateway_serves_ray_client_until_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "SERVICE_ACCOUNT", tmp_path)
    monkeypatch.setattr(
        gw,
        "ssl",
        SimpleNamespace(
            create_default_context=lambda cafile: ssl.create_default_context()
        ),
    )
    monkeypatch.setattr(gw, "SERVICE_TOKEN_FILE", tmp_path / "hub-token")
    port = free_port()
    monkeypatch.setattr(gw, "CLIENT_PORT", port)
    serving = asyncio.create_task(gw.serve())
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        await channel.channel_ready()
        with pytest.raises(grpc.aio.AioRpcError) as e:
            async for _ in channel.stream_stream(PING)(iter([b"ping"])):
                pass
    assert e.value.code() == grpc.StatusCode.UNAVAILABLE
    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving


def test_ldap_ids_reads_the_account_at_its_dn(monkeypatch):
    searched = []

    class Connection:
        def __init__(self, server, version, authentication):
            self.entries = []

        def start_tls(self):
            pass

        def search(self, search_base, search_filter, search_scope, attributes):
            searched.append((search_base, search_scope))
            attrs = {"uidNumber": 5001, "gidNumber": 500}
            self.entries = (
                [{"attributes": attrs}] if search_base.startswith("uid=user-a,") else []
            )

        def response_to_json(self):
            return json.dumps({"entries": self.entries})

    fake = types.SimpleNamespace(
        BASE="BASE", Connection=Connection, Server=lambda **kw: kw
    )
    monkeypatch.setitem(sys.modules, "ldap3", fake)

    assert gw.ldap_ids("user-a") == (5001, 500)
    assert searched == [(f"uid=user-a,{gw.LDAP_BASE}", "BASE")]
    with pytest.raises(gw.Refused) as e:
        gw.ldap_ids("nobody")
    assert e.value.code == grpc.StatusCode.PERMISSION_DENIED


def test_a_clusters_head_is_its_kuberay_service():
    assert (
        gw.head_url("ray-train-user-a")
        == "http://ray-train-user-a-head-svc.cms.svc.cluster.local:8265"
    )
    assert (
        gw.client_address("ray-train-user-a")
        == f"ray-train-user-a-head-svc.cms.svc.cluster.local:{gw.CLIENT_PORT}"
    )


def test_accounts_from_outside_purdue_map_onto_the_pool():
    assert gw.pooled_account(42) == "paf0042"
    assert gw.pooled_account(3) == "paf0003"
    with pytest.raises(gw.Refused) as e:
        gw.pooled_account(400)
    assert e.value.code == grpc.StatusCode.PERMISSION_DENIED


def test_a_user_without_a_running_session_has_no_af_id():
    assert gw.session_af_id({"name": "user-a", "servers": {}}) is None
    assert gw.session_af_id({"name": "user-a"}) is None


@pytest.mark.parametrize(
    "header, token",
    [
        ("Bearer abc", "abc"),
        ("token abc", "abc"),
        ("bearer  abc ", "abc"),
        ("Basic abc", None),
        ("Bearer", None),
        ("", None),
    ],
)
def test_bearer_token(header, token):
    assert gw.bearer_token(header) == token


def test_building_a_cluster_leaves_the_template_alone():
    template = yaml.safe_load(TEMPLATE.read_text())
    before = copy.deepcopy(template)
    user = gw.User(name="user-a", account="user-a", uid=5001, gid=500)
    gw.build_cluster(template, user, ENV_A)
    assert template == before


def test_each_cluster_has_its_own_token():
    assert gw.cluster_token("k", "ray-train-user-a") != gw.cluster_token(
        "k", "ray-train-user-b-cern"
    )
    assert gw.cluster_token("k", "ray-train-user-a") != gw.cluster_token(
        "other", "ray-train-user-a"
    )


@pytest.mark.parametrize("username", ["user-a", "user-b-cern", "a1", "x" * 43])
def test_a_cluster_is_named_after_its_user(username):
    assert gw.cluster_name(username) == f"ray-train-{username}"


@pytest.mark.parametrize("username", ["john.doe", "a_b", "trail-", "x" * 44, "日本"])
def test_a_username_that_cannot_be_a_name_is_hashed_into_one(username):
    name = gw.cluster_name(username)
    assert len(name) <= gw.MAX_CLUSTER_NAME
    assert gw.DNS_LABEL.fullmatch(name)
    assert name != gw.cluster_name(username + "x")


def test_usernames_that_slug_alike_keep_their_own_clusters():
    assert gw.cluster_name("a_b") != gw.cluster_name("a.b")
