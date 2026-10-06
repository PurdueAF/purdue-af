"""Tests for apps/ray-train/gateway.py against fake JupyterHub, Kubernetes and Ray heads.

What the isolation rests on: a call, of Ray Client's or of the Jobs API's, is
resolved to a user only by the Hub, it reaches that user's cluster and no
other, the cluster runs as the user's LDAP account, and a head only ever sees
its own derived token, never a session's.
"""

import asyncio
import copy
import json
import logging
import os
import signal
import socket
import ssl
import sys
import time
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import grpc
import pytest
import yaml
from aiohttp import (
    ClientConnectionError,
    ClientSession,
    WSMsgType,
    WSServerHandshakeError,
    web,
)
from aiohttp.test_utils import TestServer
from common import REPO, load_script

# gpu_queries.py is in the gateway's ConfigMap, beside gateway.py.
sys.path.insert(0, str(REPO / "apps" / "jupyterhub" / "jupyterhub" / "extraFiles"))
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
T4 = "nvidia.com/gpu"
SLICE = "nvidia.com/mig-1g.5gb"
# The heads' own, whatever a test makes of the gateway's.
HeadWebSocket = web.WebSocketResponse
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
        # Clusters whose deletion Kubernetes refuses
        self.reject_delete = set()
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
            if name in self.reject_delete:
                return web.json_response({"message": "forbidden"}, status=403)
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


class FakePrometheus:
    """The T4s of the AF nodes, and those some pod holds; the A100 slices likewise."""

    def __init__(self):
        self.up = True
        self.allocatable = 8
        self.used = 0
        # Of the used, those preemptible workers hold.
        self.preemptible = 0
        self.slices = 0
        self.slices_used = 0
        self.slices_preemptible = 0
        # Without the series of kube-state-metrics
        self.empty = False

    def app(self):
        async def query(request):
            if not self.up:
                return web.json_response({}, status=503)
            counts = {
                gw.ALLOC_QUERY: self.allocatable,
                gw.USED_QUERY: self.used - self.preemptible,
                gw.PREEMPTIBLE_QUERY: self.preemptible,
            }
            slices = {
                gw.ALLOC_QUERY: self.slices,
                gw.USED_QUERY: self.slices_used - self.slices_preemptible,
                gw.PREEMPTIBLE_QUERY: self.slices_preemptible,
            }
            query = request.query["query"]
            samples = [
                {"metric": {"resource": metric}, "value": [0, str(value)]}
                for metric, value in (
                    ("nvidia_com_gpu", counts[query]),
                    ("nvidia_com_mig_1g_5gb", slices[query]),
                )
            ]
            result = [] if self.empty else samples
            return web.json_response({"status": "success", "data": {"result": result}})

        app = web.Application()
        app.router.add_get("/api/v1/query", query)
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
        # Clusters whose task listing comes back in a shape no Ray gives
        self.malformed = set()
        # (method, metadata) of every call a head's Ray Client server took
        self.calls = []
        # Holds back a log stream's second line.
        self.more_logs = asyncio.Event()
        # cluster -> its jobs, as the Jobs API lists them
        self.jobs = {}
        # (cluster, package) -> what was uploaded
        self.packages = {}
        # (cluster, method, raw path, headers, body) of every call to a dashboard but the gateway's checks
        self.seen = []
        # Holds back a log tail's second line.
        self.more_lines = asyncio.Event()

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
            checks = ("/api/version", "/api/v0/tasks", "/api/jobs/")
            if not (request.method == "GET" and request.path.endswith(checks)):
                body = await request.read()
                self.seen.append(
                    (
                        cluster,
                        request.method,
                        request.rel_url.raw_path_qs,
                        dict(request.headers),
                        body,
                    )
                )
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
            if request.match_info["cluster"] in self.malformed:
                return web.json_response({})
            running = self.running.get(request.match_info["cluster"], [])
            limit = int(request.query["limit"])
            listing = {"total": len(running), "result": running[:limit]}
            return web.json_response({"result": True, "data": {"result": listing}})

        def found(request):
            cluster = request.match_info["cluster"]
            return {j["submission_id"]: j for j in self.jobs.get(cluster, [])}

        async def list_jobs(request):
            return web.json_response(self.jobs.get(request.match_info["cluster"], []))

        async def submit(request):
            return web.json_response({"submission_id": "raysubmit_1"})

        async def job(request):
            submission = request.match_info["job"]
            if submission not in found(request):
                return web.Response(status=404, text=f"Job {submission} does not exist")
            return web.json_response(found(request)[submission])

        async def package(request):
            key = (request.match_info["cluster"], request.match_info["package"])
            if request.method == "PUT":
                self.packages[key] = await request.read()
                return web.Response()
            return web.Response(status=200 if key in self.packages else 404)

        async def tail(request):
            submission = request.match_info["job"]
            if submission not in found(request):
                return web.Response(status=404, text=f"Job {submission} does not exist")
            ws = HeadWebSocket()
            await ws.prepare(request)
            await ws.send_str("line 1\n")
            if found(request)[submission].get("binary"):
                await ws.send_bytes(b"not a line")
            await self.more_lines.wait()
            await ws.send_str("line 2\n")
            await ws.close()
            return ws

        # As Ray's: working_dir uploads may reach 100 MiB.
        app = web.Application(middlewares=[check], client_max_size=100 * 2**20)
        app.router.add_get("/{cluster}/api/version", version)
        app.router.add_get("/{cluster}/api/v0/tasks", tasks)
        app.router.add_get("/{cluster}/api/jobs/", list_jobs)
        app.router.add_post("/{cluster}/api/jobs/", submit)
        app.router.add_get("/{cluster}/api/jobs/{job}", job)
        app.router.add_get("/{cluster}/api/jobs/{job}/logs/tail", tail)
        app.router.add_route("*", "/{cluster}/api/packages/gcs/{package}", package)
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
    kube, heads, prometheus = FakeKube(), FakeHeads(), FakePrometheus()
    servers = [
        TestServer(hub_app()),
        TestServer(kube.app()),
        TestServer(heads.dashboards()),
        TestServer(prometheus.app()),
    ]
    for server in servers:
        await server.start_server()
    hub, kube_server, dashboards, prometheus_server = servers
    monkeypatch.setattr(
        gw, "PROMETHEUS_URL", str(prometheus_server.make_url("")).rstrip("/")
    )
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

    async with (
        ClientSession() as http,
        ClientSession() as kube_http,
        ClientSession() as session,
    ):
        gateway = gw.Gateway(http, kube_http, token_file)
        server = grpc.aio.server(options=gw.GRPC_OPTIONS)
        server.add_generic_rpc_handlers((gw.Relay(gateway),))
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        dashboard = TestServer(gw.dashboard_app(gateway))
        await dashboard.start_server()
        yield SimpleNamespace(
            channel=channel,
            dashboard=str(dashboard.make_url("")).rstrip("/"),
            session=session,
            gateway=gateway,
            kube=kube,
            heads=heads,
            prometheus=prometheus,
            token_file=token_file,
        )
        await dashboard.close()
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
    gpus=None,
    memory=None,
):
    """A Ray Client call from a session, as the stream of raw messages the gateway sees."""
    metadata = [("client_id", client)]
    if token is not None:
        metadata.append(("authorization", f"Bearer {token}"))
    if env_path is not None:
        metadata.append((gw.ENV_METADATA, env_path))
    if gpus is not None:
        metadata.append((gw.GPUS_METADATA, gpus))
    if memory is not None:
        metadata.append((gw.MEMORY_METADATA, memory))
    responses = env.channel.stream_stream(method)(iter(messages), metadata=metadata)
    return [response async for response in responses]


async def refused(env, **kwargs):
    with pytest.raises(grpc.aio.AioRpcError) as e:
        await call(env, **kwargs)
    return e.value.code()


def job_headers(token="session-a", env_path=None, gpus=None, memory=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if env_path is not None:
        headers[gw.ENV_METADATA] = env_path
    if gpus is not None:
        headers[gw.GPUS_METADATA] = gpus
    if memory is not None:
        headers[gw.MEMORY_METADATA] = memory
    return headers


async def job_call(env, method="GET", path="/api/jobs/", body=None, **kwargs):
    """A call of JobSubmissionClient's from a session: its status, reason and body."""
    async with env.session.request(
        method, env.dashboard + path, data=body, headers=job_headers(**kwargs)
    ) as r:
        return r.status, r.reason, await r.read()


async def submit(env, **kwargs):
    return await job_call(
        env,
        "POST",
        "/api/jobs/",
        json.dumps({"entrypoint": "python train.py"}),
        **kwargs,
    )


def running_job(submission="raysubmit_1", **fields):
    return {
        "type": "SUBMISSION",
        "submission_id": submission,
        "status": "RUNNING",
    } | fields


def head_pod(env, name="ray-train-user-a"):
    return env.kube.clusters[name]["spec"]["headGroupSpec"]["template"]["spec"]


def head_env(env, name="ray-train-user-a"):
    (container,) = head_pod(env, name)["containers"]
    return {v["name"]: v["value"] for v in container["env"]}


def cluster_env(env, name="ray-train-user-a"):
    return env.kube.clusters[name]["metadata"]["annotations"][gw.ENV_ANNOTATION]


def cluster_spec(env, name="ray-train-user-a"):
    return env.kube.clusters[name]["spec"]


def workers(env, name="ray-train-user-a"):
    """The worker that keeps its GPU."""
    return cluster_spec(env, name)["workerGroupSpecs"][0]


def preemptible(env, name="ray-train-user-a"):
    """The workers that give their GPUs up to pods of the default priority."""
    (group,) = cluster_spec(env, name)["workerGroupSpecs"][1:]
    return group


def gpus(env, name="ray-train-user-a"):
    """The workers of each group, by its name, and the GPU resource of each."""
    held = {}
    for group in cluster_spec(env, name)["workerGroupSpecs"]:
        (container,) = group["template"]["spec"]["containers"]
        (resource,) = (r for r in container["resources"]["limits"] if "nvidia" in r)
        assert container["resources"]["requests"][resource] == 1
        held[group["groupName"]] = (group["replicas"], resource)
    return held


def counts(group):
    return group["replicas"], group["minReplicas"], group["maxReplicas"]


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


def provisioning(env, name="ray-train-user-a", age=0.0, provisioned="False"):
    """The cluster as KubeRay reports it `age` seconds in, its pods not all ready yet."""
    created = datetime.fromtimestamp(time.time() - age, timezone.utc)
    cluster = env.kube.clusters[name]
    cluster["metadata"]["creationTimestamp"] = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    cluster["status"] = {
        "conditions": [
            {"type": "HeadPodReady", "status": "True"},
            {"type": "RayClusterProvisioned", "status": provisioned},
        ]
    }


def scraped(env):
    """Prometheus has seen every admitted cluster's pods."""
    env.gateway.grants.clear()


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
    await call(env, env_path=ENV_A, gpus="2")
    ((method, metadata),) = env.heads.calls
    assert method == PING
    assert metadata["authorization"] == (
        f"Bearer {gw.cluster_token(SERVICE_TOKEN, 'ray-train-user-a')}"
    )
    assert metadata["client_id"] == "client-1"
    assert gw.ENV_METADATA not in metadata
    assert gw.GPUS_METADATA not in metadata
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


async def test_a_cluster_holds_one_gpu_unless_asked_for_more(env):
    await call(env)
    (group,) = cluster_spec(env)["workerGroupSpecs"]
    assert counts(group) == (1, 1, 1)
    assert "priorityClassName" not in group["template"]["spec"]


async def test_gpus_asked_for_are_held_from_the_start(env):
    await call(env, gpus="4")
    assert counts(workers(env)) == (1, 1, 1)
    assert counts(preemptible(env)) == (3, 3, 3)
    annotations = env.kube.clusters["ray-train-user-a"]["metadata"]["annotations"]
    assert annotations[gw.GPUS_ANNOTATION] == "4"


async def test_only_the_gpus_past_the_first_are_preemptible(env):
    await call(env, gpus="3")
    first, rest = workers(env), preemptible(env)
    assert first["groupName"] != rest["groupName"]
    assert "priorityClassName" not in first["template"]["spec"]
    assert (
        rest["template"]["spec"].pop("priorityClassName")
        == gw.PREEMPTIBLE_PRIORITY_CLASS
    )
    assert rest["template"] == first["template"]


async def test_head_and_workers_run_as_the_user_in_the_environment(env):
    await call(env, gpus="2", env_path=ENV_A)
    head = cluster_spec(env)["headGroupSpec"]["template"]
    worker = workers(env)["template"]
    (head_container,) = head["spec"]["containers"]
    (worker_container,) = worker["spec"]["containers"]
    assert worker_container["env"] == head_container["env"]
    assert any(v["value"].startswith(f"{ENV_A}/bin:") for v in worker_container["env"])
    assert worker["spec"]["securityContext"] == head["spec"]["securityContext"]
    assert worker["spec"]["securityContext"]["runAsUser"] == 5001


@pytest.mark.parametrize("gpus", ["0", "23", "two", "1.5", "auto"])
async def test_a_gpu_count_out_of_range_is_refused(env, gpus):
    assert await refused(env, gpus=gpus) == grpc.StatusCode.INVALID_ARGUMENT
    assert env.kube.clusters == {}


async def test_asking_for_other_gpus_replaces_an_idle_cluster(env):
    await call(env)
    assert await call(env, gpus="3") == [b"echo:ping"]
    assert counts(preemptible(env)) == (2, 2, 2)


async def test_asking_for_other_gpus_leaves_a_busy_cluster_alone(env):
    await call(env)
    env.heads.running["ray-train-user-a"] = [{"task_id": "t", "state": "RUNNING"}]
    assert await refused(env, gpus="2") == grpc.StatusCode.FAILED_PRECONDITION
    assert counts(workers(env)) == (1, 1, 1)


async def test_a_cluster_asking_for_more_gpus_than_are_free_is_refused(env):
    env.prometheus.used = 6
    assert await refused(env, gpus="3") == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert env.kube.clusters == {}
    assert await call(env, gpus="2") == [b"echo:ping"]


async def test_a_refused_submission_says_how_many_gpus_are_free(env):
    env.prometheus.used = 7
    status, reason, _ = await submit(env, gpus="4")
    assert status == 429
    assert "asks for 4 GPUs of 5 GB or more and Ray clusters can take 1 more" in reason
    assert env.kube.clusters == {}


async def test_gpus_just_admitted_count_as_taken_until_prometheus_sees_them(env):
    env.prometheus.used = 4
    await call(env, gpus="3")
    assert (
        await refused(env, token="session-b", gpus="2")
        == grpc.StatusCode.RESOURCE_EXHAUSTED
    )
    assert await call(env, token="session-b", client="client-b") == [b"echo:ping"]


async def test_one_cluster_may_hold_every_gpu(env):
    await call(env, gpus="8")
    assert counts(preemptible(env)) == (7, 7, 7)


async def test_a_cluster_of_one_gpu_takes_it_from_a_preemptible_worker(env):
    await call(env, gpus="8")
    scraped(env)
    env.prometheus.used, env.prometheus.preemptible = 8, 7
    assert (
        await refused(env, token="session-b", gpus="2")
        == grpc.StatusCode.RESOURCE_EXHAUSTED
    )
    assert await call(env, token="session-b", client="client-b") == [b"echo:ping"]
    assert live(env) == {"ray-train-user-a", "ray-train-user-b-cern"}


async def test_no_cluster_takes_a_gpu_from_a_pod_that_keeps_it(env):
    env.prometheus.used = 8
    assert await refused(env) == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert env.kube.clusters == {}


async def test_a_preemptible_gpu_is_taken_by_one_cluster_only(env):
    env.prometheus.used, env.prometheus.preemptible = 8, 1
    await call(env)
    assert await refused(env, token="session-b") == grpc.StatusCode.RESOURCE_EXHAUSTED


async def test_a_replacement_takes_no_gpu_of_its_predecessors_workers(env):
    await call(env, gpus="3")
    scraped(env)
    env.prometheus.used, env.prometheus.preemptible = 8, 2
    assert await refused(env, gpus="4") == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert live(env) == {"ray-train-user-a"}


async def test_a_replacement_may_take_the_gpus_its_predecessor_holds(env):
    await call(env, gpus="2")
    scraped(env)
    env.prometheus.used = 6
    assert await call(env, gpus="4") == [b"echo:ping"]
    assert counts(preemptible(env)) == (3, 3, 3)
    # Prometheus has not seen the four yet.
    assert (
        await refused(env, token="session-b", gpus="3")
        == grpc.StatusCode.RESOURCE_EXHAUSTED
    )


async def test_a_refused_replacement_leaves_the_cluster_it_would_replace(env):
    await call(env, gpus="2")
    scraped(env)
    env.prometheus.used = 8
    assert await refused(env, gpus="3") == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert live(env) == {"ray-train-user-a"}
    assert counts(preemptible(env)) == (1, 1, 1)


@pytest.mark.parametrize("failure", ["up", "empty"])
async def test_without_prometheus_only_the_budget_holds(env, monkeypatch, failure):
    setattr(env.prometheus, failure, failure == "empty")
    env.prometheus.used = 8
    assert await call(env, gpus="6", memory="16") == [b"echo:ping"]
    assert (
        await refused(env, token="session-b", gpus="3", memory="16")
        == grpc.StatusCode.RESOURCE_EXHAUSTED
    )
    assert await call(env, token="session-b", client="client-b", gpus="3") == [
        b"echo:ping"
    ]
    assert gpus(env, "ray-train-user-b-cern") == {
        "mig-1g-5gb": (1, SLICE),
        "mig-1g-5gb-preemptible": (2, SLICE),
    }


async def test_small_gpus_are_taken_before_large_ones(env):
    env.prometheus.slices = 14
    env.prometheus.slices_used = 12
    await call(env, gpus="5")
    assert gpus(env) == {
        "mig-1g-5gb": (1, SLICE),
        "mig-1g-5gb-preemptible": (1, SLICE),
        "gpu-preemptible": (3, T4),
    }
    annotations = env.kube.clusters["ray-train-user-a"]["metadata"]["annotations"]
    assert annotations[gw.MEMORY_ANNOTATION] == "5"


@pytest.mark.parametrize("memory", ["0", "2.5", "5"])
async def test_a_worker_that_needs_little_memory_may_hold_any_gpu(env, memory):
    env.prometheus.slices = 14
    await call(env, gpus="22", memory=memory)
    assert gpus(env) == {
        "mig-1g-5gb": (1, SLICE),
        "mig-1g-5gb-preemptible": (13, SLICE),
        "gpu-preemptible": (8, T4),
    }


@pytest.mark.parametrize("memory", ["5.5", "8", "16"])
async def test_a_worker_that_needs_more_memory_than_a_slice_holds_a_t4(env, memory):
    env.prometheus.slices = 14
    assert (
        await refused(env, gpus="9", memory=memory) == grpc.StatusCode.INVALID_ARGUMENT
    )
    await call(env, gpus="8", memory=memory)
    assert gpus(env) == {"gpu": (1, T4), "gpu-preemptible": (7, T4)}
    annotations = env.kube.clusters["ray-train-user-a"]["metadata"]["annotations"]
    assert annotations[gw.MEMORY_ANNOTATION] == "16"


@pytest.mark.parametrize("memory", ["16.5", "40", "inf", "nan", "lots"])
async def test_memory_that_no_gpu_has_is_refused(env, memory):
    env.prometheus.slices = 14
    assert await refused(env, memory=memory) == grpc.StatusCode.INVALID_ARGUMENT
    assert env.kube.clusters == {}


async def test_asking_for_more_memory_replaces_an_idle_cluster_of_slices(env):
    env.prometheus.slices = 14
    await call(env)
    assert gpus(env) == {"mig-1g-5gb": (1, SLICE)}
    await call(env, memory="4")
    assert env.kube.created == 1
    await call(env, memory="10")
    assert env.kube.created == 2
    assert gpus(env) == {"gpu": (1, T4)}


async def test_a_cluster_from_before_the_memory_setting_serves_any_call(env):
    await call(env)
    del env.kube.clusters["ray-train-user-a"]["metadata"]["annotations"][
        gw.MEMORY_ANNOTATION
    ]
    env.gateway.started.clear()
    await call(env)
    assert env.kube.created == 1


async def test_clusters_count_the_gpus_of_each_kind_just_admitted(env):
    env.prometheus.slices = 2
    await call(env, gpus="2")
    await call(env, token="session-b", client="client-b", gpus="2")
    assert gpus(env, "ray-train-user-b-cern") == {
        "gpu": (1, T4),
        "gpu-preemptible": (1, T4),
    }


async def test_a_cluster_of_one_gpu_takes_a_preemptible_slice(env):
    env.prometheus.used = 8
    env.prometheus.slices = env.prometheus.slices_used = 14
    assert await refused(env) == grpc.StatusCode.RESOURCE_EXHAUSTED
    env.prometheus.slices_preemptible = 3
    assert await refused(env, memory="16") == grpc.StatusCode.RESOURCE_EXHAUSTED
    await call(env)
    assert gpus(env) == {"mig-1g-5gb": (1, SLICE)}


async def test_admission_needs_the_listing_of_clusters(env):
    env.kube.fail_list = True
    assert await refused(env) == grpc.StatusCode.UNAVAILABLE
    assert env.kube.clusters == {}


async def test_a_cluster_whose_workers_never_all_start_is_removed(env):
    await call(env, gpus="4")
    env.heads.jobs["ray-train-user-a"] = [running_job()]
    provisioning(env, age=gw.PROVISION_TIMEOUT_S + 1)

    await env.gateway.reap_idle()

    assert live(env) == set()
    assert await refused(env) == grpc.StatusCode.NOT_FOUND
    status, reason, _ = await job_call(env, path="/api/jobs/raysubmit_1")
    assert status == 404
    assert "not all of its GPUs started" in reason


async def test_the_next_cluster_forgets_why_the_last_was_removed(env):
    await call(env, gpus="4")
    provisioning(env, age=gw.PROVISION_TIMEOUT_S + 1)
    await env.gateway.reap_idle()
    await call(env, client="client-2")
    assert env.gateway.never_started == {}


async def test_a_cluster_still_within_its_time_to_start_is_kept(env):
    await call(env, gpus="4")
    provisioning(env, age=gw.PROVISION_TIMEOUT_S - 60)
    await env.gateway.reap_idle()
    assert live(env) == {"ray-train-user-a"}


@pytest.mark.parametrize("provisioned", ["True", None])
async def test_a_cluster_that_started_is_never_removed_for_a_worker_lost_later(
    env, provisioned
):
    """KubeRay sets RayClusterProvisioned once, and an operator without the
    condition says nothing about it."""
    await call(env, gpus="4")
    provisioning(env, age=gw.PROVISION_TIMEOUT_S + 1, provisioned=provisioned)
    await env.gateway.reap_idle()
    assert live(env) == {"ray-train-user-a"}


async def test_a_job_client_without_a_token_is_refused(env):
    status, _, _ = await job_call(env, path="/api/version", token=None)
    assert status == 401
    assert env.kube.clusters == {}


async def test_the_version_check_starts_no_cluster(env):
    status, _, body = await job_call(env, path="/api/version")
    assert status == 200
    template = yaml.safe_load(TEMPLATE.read_text())
    assert json.loads(body) == {"ray_version": template["spec"]["rayVersion"]}
    assert env.kube.clusters == {}


async def test_a_refusal_says_why_in_its_status_line(env):
    """JobSubmissionClient's version check shows a refusal's status line only."""
    status, reason, body = await job_call(env, path="/api/version", gpus="23")
    assert status == 400
    assert reason == body.decode()
    assert reason.startswith(gw.GPUS_METADATA)


async def test_submitting_a_job_starts_the_users_cluster_in_the_shape_it_names(env):
    status, _, body = await submit(env, env_path=ENV_A, gpus="2")
    assert (status, json.loads(body)) == (200, {"submission_id": "raysubmit_1"})
    assert cluster_env(env) == ENV_A
    assert counts(preemptible(env)) == (1, 1, 1)
    ((cluster, method, path, headers, sent),) = env.heads.seen
    assert (cluster, method, path) == (
        "ray-train-user-a",
        "POST",
        "/ray-train-user-a/api/jobs/",
    )
    assert headers["Authorization"] == (
        f"Bearer {gw.cluster_token(SERVICE_TOKEN, 'ray-train-user-a')}"
    )
    assert not {gw.ENV_METADATA, gw.GPUS_METADATA} & {k.lower() for k in headers}
    assert "session-a" not in json.dumps(headers)
    assert json.loads(sent) == {"entrypoint": "python train.py"}


async def test_an_upload_reaches_the_head_whole(env):
    package = os.urandom(3 * 2**20)
    path = "/api/packages/gcs/_ray_pkg_1.zip"
    assert (await job_call(env, "GET", path))[0] == 404
    assert (await job_call(env, "PUT", path, package))[0] == 200
    assert env.heads.packages[("ray-train-user-a", "_ray_pkg_1.zip")] == package
    assert (await job_call(env, "GET", path))[0] == 200


@pytest.mark.parametrize(
    "path", ["/api/jobs/", "/api/jobs/raysubmit_1", "/api/jobs/raysubmit_1/logs"]
)
async def test_asking_about_jobs_without_a_cluster_starts_none(env, path):
    status, _, body = await job_call(env, path=path)
    assert status == 404
    assert body.startswith(b"You have no Ray cluster")
    assert env.kube.clusters == {}


async def test_asking_about_jobs_reaches_the_cluster_whatever_it_runs(env):
    await submit(env, env_path=ENV_A, gpus="2")
    env.heads.jobs["ray-train-user-a"] = [running_job()]
    # As after the gateway restarted
    env.gateway.started.clear()
    status, _, body = await job_call(env, path="/api/jobs/raysubmit_1")
    assert (status, json.loads(body)["status"]) == (200, "RUNNING")
    assert cluster_env(env) == ENV_A
    assert env.kube.created == 1


async def test_a_cluster_being_deleted_has_no_jobs_to_ask_about(env):
    await submit(env)
    env.gateway.started.clear()
    env.kube.clusters["ray-train-user-a"]["metadata"]["deletionTimestamp"] = "now"
    assert (await job_call(env, path="/api/jobs/raysubmit_1"))[0] == 404


async def test_a_heads_answer_comes_back_as_it_is(env):
    await submit(env)
    status, _, body = await job_call(env, path="/api/jobs/raysubmit_9")
    assert (status, body) == (404, b"Job raysubmit_9 does not exist")


async def test_paths_and_queries_reach_the_head_as_sent(env):
    await submit(env)
    await job_call(env, path="/api/jobs/my%20job%231?verbose=1")
    assert env.heads.seen[-1][1:3] == (
        "GET",
        "/ray-train-user-a/api/jobs/my%20job%231?verbose=1",
    )


async def test_a_submission_in_another_shape_leaves_a_cluster_with_a_running_job(env):
    await submit(env, env_path=ENV_A)
    env.heads.jobs["ray-train-user-a"] = [running_job()]
    status, reason, _ = await submit(env, env_path=ENV_B)
    assert status == 409
    assert "tasks or jobs" in reason
    assert cluster_env(env) == ENV_A


async def test_a_submission_in_another_shape_replaces_a_cluster_whose_jobs_ended(env):
    await submit(env, env_path=ENV_A)
    env.heads.jobs["ray-train-user-a"] = [running_job(status="SUCCEEDED")]
    assert (await submit(env, env_path=ENV_B))[0] == 200
    assert cluster_env(env) == ENV_B


@pytest.mark.parametrize(
    "job, kept",
    [
        (running_job(), True),
        (running_job(status="PENDING"), True),
        (running_job(status="SUCCEEDED"), False),
        # A notebook's Ray Client session, which keeps no cluster by itself
        (running_job(type="DRIVER"), False),
    ],
    ids=["running", "pending", "ended", "ray-client-driver"],
)
async def test_a_submitted_job_keeps_its_cluster_until_it_ends(env, job, kept):
    await submit(env)
    env.heads.jobs["ray-train-user-a"] = [job]
    idle(env, "ray-train-user-a")
    await env.gateway.reap_idle()
    assert (live(env) == {"ray-train-user-a"}) is kept


async def test_a_cluster_stays_for_the_idle_timeout_after_its_last_job_ends(env):
    await submit(env)
    env.heads.jobs["ray-train-user-a"] = [running_job()]
    idle(env, "ray-train-user-a")
    await env.gateway.reap_idle()
    env.heads.jobs["ray-train-user-a"] = [running_job(status="SUCCEEDED")]
    await env.gateway.reap_idle()
    assert live(env) == {"ray-train-user-a"}
    idle(env, "ray-train-user-a")
    await env.gateway.reap_idle()
    assert live(env) == set()


async def test_asking_about_jobs_keeps_a_cluster(env):
    await submit(env)
    idle(env, "ray-train-user-a")
    await job_call(env, path="/api/jobs/raysubmit_1")
    await env.gateway.reap_idle()
    assert live(env) == {"ray-train-user-a"}


async def tail(env, submission="raysubmit_1"):
    """JobSubmissionClient.tail_job_logs, as the lines it yields."""
    lines = []
    url = f"{env.dashboard}/api/jobs/{submission}/logs/tail"
    async with env.session.ws_connect(url, headers=job_headers()) as ws:
        async for message in ws:
            assert message.type == WSMsgType.TEXT
            lines.append(message.data)
            env.heads.more_lines.set()
    return lines


async def test_a_jobs_log_tail_is_relayed_until_the_job_ends(env):
    await submit(env)
    env.heads.jobs["ray-train-user-a"] = [running_job()]
    assert await tail(env) == ["line 1\n", "line 2\n"]


async def test_a_log_tail_ends_at_anything_but_a_line(env):
    await submit(env)
    env.heads.jobs["ray-train-user-a"] = [running_job(binary=True)]
    assert await tail(env) == ["line 1\n"]


async def test_a_tail_to_a_session_gone_without_a_word_ends_quietly(
    env, monkeypatch, caplog
):
    """A session pod that vanished leaves its connection half open: only a write finds out."""

    class HalfOpen(web.WebSocketResponse):
        async def send_str(self, data, compress=None):
            raise ConnectionResetError("Cannot write to closing transport")

    monkeypatch.setattr(web, "WebSocketResponse", HalfOpen)
    await submit(env)
    env.heads.jobs["ray-train-user-a"] = [running_job()]
    env.heads.more_lines.set()
    assert await tail(env) == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_the_log_tail_of_an_unknown_job_is_refused(env):
    await submit(env)
    with pytest.raises(WSServerHandshakeError) as e:
        await tail(env, "raysubmit_9")
    assert e.value.status == 404


@pytest.mark.parametrize("call", ["http", "websocket"])
async def test_an_unreachable_head_is_reported_and_checked_again(
    env, monkeypatch, call
):
    await submit(env)
    port = free_port()
    monkeypatch.setattr(gw, "head_url", lambda cluster: f"http://127.0.0.1:{port}")
    if call == "http":
        assert (await job_call(env, path="/api/jobs/raysubmit_1"))[0] == 503
    else:
        with pytest.raises(WSServerHandshakeError) as e:
            await tail(env)
        assert e.value.status == 503
    assert "ray-train-user-a" not in env.gateway.started


def test_environments_live_on_the_storage_the_cluster_mounts():
    template = yaml.safe_load(TEMPLATE.read_text())
    assert gw.env_roots(template) == ["/work", "/depot/cms", "/eos", "/cvmfs"]
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


async def test_a_ready_head_answering_out_of_shape_is_kept_and_not_replaced(env):
    await call(env)
    env.kube.clusters["ray-train-user-a"]["status"] = {
        "conditions": [{"type": "HeadPodReady", "status": "True"}]
    }
    env.heads.malformed.add("ray-train-user-a")
    idle(env, "ray-train-user-a")

    await env.gateway.reap_idle()

    assert "ray-train-user-a" in live(env)
    assert await refused(env, gpus="2") == grpc.StatusCode.FAILED_PRECONDITION


async def test_a_refused_deletion_is_tried_again_and_holds_up_no_other_cluster(env):
    await call(env, token="session-a")
    await call(env, token="session-b", client="client-b")
    # Listed first, so the rest are reaped only past it.
    env.kube.reject_delete.add("ray-train-user-a")
    idle(env, "ray-train-user-a", "ray-train-user-b-cern")

    await env.gateway.reap_idle()

    assert live(env) == {"ray-train-user-a"}
    assert await call(env) == [b"echo:ping"]
    idle(env, "ray-train-user-a")
    env.kube.reject_delete.clear()
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


async def test_the_gateway_serves_both_ports_until_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "SERVICE_ACCOUNT", tmp_path)
    monkeypatch.setattr(
        gw,
        "ssl",
        SimpleNamespace(
            create_default_context=lambda cafile: ssl.create_default_context()
        ),
    )
    monkeypatch.setattr(gw, "SERVICE_TOKEN_FILE", tmp_path / "hub-token")
    port, dashboard_port = free_port(), free_port()
    monkeypatch.setattr(gw, "CLIENT_PORT", port)
    monkeypatch.setattr(gw, "DASHBOARD_PORT", dashboard_port)
    serving = asyncio.create_task(gw.serve())
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        await channel.channel_ready()
        with pytest.raises(grpc.aio.AioRpcError) as e:
            async for _ in channel.stream_stream(PING)(iter([b"ping"])):
                pass
    assert e.value.code() == grpc.StatusCode.UNAVAILABLE
    async with ClientSession() as session:
        while True:
            try:
                async with session.get(
                    f"http://127.0.0.1:{dashboard_port}/api/version"
                ) as r:
                    assert r.status == 503
                    break
            except ClientConnectionError:
                await asyncio.sleep(0.05)
    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving


async def test_the_gateway_stops_at_sigterm(monkeypatch):
    stopped = asyncio.Event()

    async def serve():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(gw, "serve", serve)
    default = signal.getsignal(signal.SIGTERM)
    main = asyncio.create_task(gw.main())
    for _ in range(100):
        if signal.getsignal(signal.SIGTERM) is not default:
            break
        await asyncio.sleep(0)
    # Never signal the test run itself before the gateway handles it.
    assert signal.getsignal(signal.SIGTERM) is not default
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(main, timeout=5)
    assert stopped.is_set()


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


async def test_only_new_gateway_code_ends_the_gateway(monkeypatch, tmp_path):
    """A new raycluster.yaml is read at the next cluster creation; ending the
    gateway for it would end every open Ray Client connection."""
    files = {"gateway.py": "running", "raycluster.yaml": "template"}
    mount_configmap(tmp_path, 1, files)
    monkeypatch.setattr(gw, "CODE_POLL_S", 0)
    changed = asyncio.create_task(gw.until_changed(tmp_path / "gateway.py", b"running"))
    mount_configmap(tmp_path, 2, {**files, "raycluster.yaml": "new template"})
    for _ in range(10):
        await asyncio.sleep(0)
    assert not changed.done()
    mount_configmap(tmp_path, 3, {**files, "gateway.py": "new code"})
    await asyncio.wait_for(changed, timeout=5)


@pytest.mark.parametrize("changing", ["CODE", "CONFIG_FILE"])
async def test_the_gateway_stops_when_its_code_or_settings_change(
    monkeypatch, tmp_path, changing
):
    """It exits cleanly, and kubelet restarts the container on the new file."""
    stopped = asyncio.Event()

    async def serve():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    code = tmp_path / "gateway.py"
    code.write_text("running")
    monkeypatch.setattr(gw, "serve", serve)
    monkeypatch.setattr(gw, changing, code)
    monkeypatch.setattr(gw, "CODE_POLL_S", 0)
    main = asyncio.create_task(gw.main())
    await asyncio.sleep(0)
    code.write_text("new code")
    await asyncio.wait_for(main, timeout=5)
    assert stopped.is_set()


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
    gw.build_cluster(template, user, gw.Shape(ENV_A, "4", "5"), {T4: 4})
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
