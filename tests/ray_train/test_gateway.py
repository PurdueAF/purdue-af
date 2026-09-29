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

import pytest
import yaml
from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from common import REPO, load_script

gw = load_script(REPO / "apps" / "ray-train" / "gateway.py", "ray_train_gateway")

TEMPLATE = REPO / "apps" / "ray-train" / "raycluster.yaml"
SERVICE_TOKEN = "service-token"
# session token -> (JupyterHub name, AF id of the running session, or None)
SESSIONS = {
    "session-a": ("user-a", 7),
    "session-b": ("user-b-cern", 42),
    "session-idle": ("user-idle", None),
}
LDAP = {"user-a": (5001, 500), "paf0042": (6042, 600)}


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def closed_port_url():
    """An address nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


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
        # Clusters another request is creating: absent to a GET, present to a POST.
        self.racing = set()

    def app(self):
        clusters = "/apis/ray.io/v1/namespaces/cms/rayclusters"

        def stored(body):
            body["metadata"]["uid"] = f"uid-{body['metadata']['name']}"
            self.clusters[body["metadata"]["name"]] = body

        async def create(request):
            body = await request.json()
            name = body["metadata"]["name"]
            if self.reject_create:
                return web.json_response({"message": "invalid spec"}, status=422)
            if name in self.racing:
                self.racing.discard(name)
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
            if name in self.racing or name not in self.clusters:
                return web.json_response({"message": "not found"}, status=404)
            return web.json_response(self.clusters[name])

        async def delete(request):
            self.clusters.pop(request.match_info["name"], None)
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
    """Every user's Ray head, at /<cluster>/...; each accepts only its own token."""

    def __init__(self):
        self.up = True
        self.down_checks = 0
        self.jobs = {}
        self.seen_tokens = []
        self.uploads = {}

    def app(self):
        @web.middleware
        async def check(request, handler):
            if not self.up:
                raise web.HTTPServiceUnavailable()
            cluster = request.match_info.get("cluster", "")
            token = request.headers.get("Authorization", "")
            self.seen_tokens.append(token)
            if token != f"Bearer {gw.cluster_token(SERVICE_TOKEN, cluster)}":
                raise web.HTTPForbidden()
            return await handler(request)

        async def version(request):
            if self.down_checks:
                self.down_checks -= 1
                raise web.HTTPServiceUnavailable()
            return web.json_response({"version": "4", "ray_version": "2.58.0"})

        async def list_jobs(request):
            return web.json_response(self.jobs.get(request.match_info["cluster"], []))

        async def submit(request):
            cluster = request.match_info["cluster"]
            body = await request.json()
            job = {"submission_id": f"raysubmit_{cluster}", "status": "RUNNING", **body}
            self.jobs.setdefault(cluster, []).append(job)
            return web.json_response({"job_id": job["submission_id"]})

        async def package(request):
            key = (request.match_info["cluster"], request.match_info["name"])
            if request.method == "PUT":
                self.uploads[key] = await request.read()
                return web.Response()
            return web.Response(status=200 if key in self.uploads else 404)

        async def tail(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str("epoch 0\n")
            await ws.send_bytes(b"epoch 1\n")
            await ws.close()
            return ws

        app = web.Application(middlewares=[check])
        app.router.add_get("/{cluster}/api/version", version)
        app.router.add_get("/{cluster}/api/jobs/", list_jobs)
        app.router.add_post("/{cluster}/api/jobs/", submit)
        app.router.add_route("*", "/{cluster}/api/packages/{protocol}/{name}", package)
        app.router.add_get("/{cluster}/api/jobs/{job}/logs/tail", tail)
        return app


@pytest.fixture
async def env(monkeypatch, tmp_path):
    kube, heads = FakeKube(), FakeHeads()
    servers = [TestServer(hub_app()), TestServer(kube.app()), TestServer(heads.app())]
    for server in servers:
        await server.start_server()
    hub, kube_server, head_server = servers
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
    heads_url = str(head_server.make_url("")).rstrip("/")
    monkeypatch.setattr(gw, "head_url", lambda cluster: f"{heads_url}/{cluster}")

    async with ClientSession() as http, ClientSession() as kube_http:
        gateway = gw.Gateway(http, kube_http, token_file)
        app = web.Application()
        app[gw.GATEWAY] = gateway
        app.router.add_route("*", "/{path:.*}", gw.handle)
        client = TestClient(TestServer(app))
        await client.start_server()
        yield SimpleNamespace(
            client=client,
            gateway=gateway,
            kube=kube,
            heads=heads,
            token_file=token_file,
        )
        await client.close()
    for server in servers:
        await server.close()


def unreachable_heads(monkeypatch):
    url = closed_port_url()
    monkeypatch.setattr(gw, "head_url", lambda cluster: url)


async def submit(env, token="session-a"):
    return await env.client.post(
        "/api/jobs/", json={"entrypoint": "python train.py"}, headers=auth(token)
    )


async def test_health_needs_no_token(env):
    r = await env.client.get("/healthz")
    assert r.status == 200


async def test_a_call_without_a_token_is_refused(env):
    r = await env.client.get("/api/jobs/")
    assert r.status == 401


async def test_a_token_the_hub_does_not_know_is_refused(env):
    r = await env.client.get("/api/jobs/", headers=auth("forged"))
    assert r.status == 401
    assert env.kube.clusters == {}


async def test_only_the_jobs_api_is_forwarded(env):
    r = await env.client.get("/api/cluster_status", headers=auth("session-a"))
    assert r.status == 404


async def test_nothing_is_served_before_the_hub_registers_the_service(env):
    env.token_file.unlink()
    r = await env.client.get("/api/version", headers=auth("session-a"))
    assert r.status == 503


async def test_the_service_token_is_read_when_it_appears(env):
    env.token_file.unlink()
    assert (
        await env.client.get("/api/version", headers=auth("session-a"))
    ).status == 503
    env.token_file.write_text(SERVICE_TOKEN)
    assert (
        await env.client.get("/api/version", headers=auth("session-a"))
    ).status == 200


async def test_version_and_listing_start_no_cluster(env):
    r = await env.client.get("/api/version", headers=auth("session-a"))
    rayversion = yaml.safe_load(TEMPLATE.read_text())["spec"]["rayVersion"]
    assert (await r.json())["ray_version"] == rayversion
    r = await env.client.get("/api/jobs/", headers=auth("session-a"))
    assert await r.json() == []
    assert env.kube.clusters == {}


async def test_a_job_without_a_cluster_is_not_found(env):
    r = await env.client.get("/api/jobs/raysubmit_x", headers=auth("session-a"))
    assert r.status == 404


async def test_submitting_starts_the_users_cluster_as_them(env):
    r = await submit(env)
    assert r.status == 200
    assert (await r.json())["job_id"] == "raysubmit_ray-train-7"

    cluster = env.kube.clusters["ray-train-7"]
    pod = cluster["spec"]["headGroupSpec"]["template"]["spec"]
    assert pod["securityContext"] == {
        "supplementalGroups": [100],
        "runAsUser": 5001,
        "runAsGroup": 500,
    }
    assert {"name": "USER", "value": "user-a"} in pod["containers"][0]["env"]
    assert cluster["spec"]["authOptions"]["secretName"] == "ray-train-7"

    secret = env.kube.secrets["ray-train-7"]
    assert secret["stringData"]["auth_token"] == gw.cluster_token(
        SERVICE_TOKEN, "ray-train-7"
    )
    assert secret["metadata"]["ownerReferences"][0]["uid"] == "uid-ray-train-7"


async def test_a_head_never_sees_a_session_token(env):
    await submit(env)
    await env.client.get("/api/jobs/", headers=auth("session-a"))
    assert env.heads.seen_tokens
    assert not any("session-a" in token for token in env.heads.seen_tokens)


async def test_an_external_user_runs_as_their_pooled_account(env):
    await submit(env, "session-b")
    pod = env.kube.clusters["ray-train-42"]["spec"]["headGroupSpec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 6042
    assert pod["securityContext"]["runAsGroup"] == 600


async def test_a_user_without_a_running_session_is_told_to_start_one(env):
    r = await submit(env, "session-idle")
    assert r.status == 409
    assert env.kube.clusters == {}


async def test_users_reach_only_their_own_cluster(env):
    await env.client.post(
        "/api/jobs/", json={"entrypoint": "a"}, headers=auth("session-a")
    )
    r = await env.client.get("/api/jobs/", headers=auth("session-b"))
    assert await r.json() == []
    await env.client.post(
        "/api/jobs/", json={"entrypoint": "b"}, headers=auth("session-b")
    )
    r = await env.client.get("/api/jobs/", headers=auth("session-b"))
    assert [job["entrypoint"] for job in await r.json()] == ["b"]
    assert set(env.kube.clusters) == {"ray-train-7", "ray-train-42"}


async def test_code_is_uploaded_to_the_users_cluster(env):
    path = "/api/packages/gcs/_ray_pkg_abc.zip"
    r = await env.client.get(path, headers=auth("session-a"))
    assert r.status == 404
    r = await env.client.put(path, data=b"zip bytes" * 1000, headers=auth("session-a"))
    assert r.status == 200
    assert env.heads.uploads[("ray-train-7", "_ray_pkg_abc.zip")] == b"zip bytes" * 1000


async def test_job_logs_stream_through(env):
    await submit(env)
    ws = await env.client.ws_connect(
        "/api/jobs/raysubmit_ray-train-7/logs/tail", headers=auth("session-a")
    )
    frames = [message.data async for message in ws if message.type != WSMsgType.CLOSE]
    assert frames == ["epoch 0\n", b"epoch 1\n"]


async def test_logs_from_an_unreachable_head_end_the_stream(env, monkeypatch):
    await submit(env)
    unreachable_heads(monkeypatch)
    ws = await env.client.ws_connect(
        "/api/jobs/raysubmit_ray-train-7/logs/tail", headers=auth("session-a")
    )
    assert [message async for message in ws] == []


async def test_a_head_that_comes_up_late_is_waited_for(env):
    env.heads.down_checks = 2
    r = await submit(env)
    assert r.status == 200


async def test_a_cluster_that_does_not_start_is_reported(env, monkeypatch):
    env.heads.up = False
    monkeypatch.setattr(gw, "START_TIMEOUT_S", 0)
    r = await submit(env)
    assert r.status == 503


async def test_an_unreachable_head_is_reported(env, monkeypatch):
    unreachable_heads(monkeypatch)
    monkeypatch.setattr(gw, "START_TIMEOUT_S", 0)
    r = await submit(env)
    assert r.status == 503


async def test_a_call_the_head_cannot_take_is_reported(env, monkeypatch):
    await submit(env)
    unreachable_heads(monkeypatch)
    r = await env.client.get("/api/jobs/", headers=auth("session-a"))
    assert r.status == 502


async def test_a_concurrent_creation_is_joined(env):
    env.kube.racing.add("ray-train-7")
    r = await submit(env)
    assert r.status == 200
    assert "ray-train-7" in env.kube.clusters


async def test_a_cluster_being_deleted_is_not_reused(env):
    await submit(env)
    env.kube.clusters["ray-train-7"]["metadata"]["deletionTimestamp"] = "now"
    r = await submit(env)
    assert r.status == 503


@pytest.mark.parametrize(
    "failure, token",
    [
        ("reject_create", "session-a"),
        ("reject_secret", "session-a"),
        ("fail_get", "session-a"),
        (None, "hub-down"),
    ],
)
async def test_upstream_failures_are_reported_as_bad_gateway(env, failure, token):
    if failure:
        setattr(env.kube, failure, True)
    r = await submit(env, token)
    assert r.status == 502


async def test_a_service_token_the_hub_refuses_is_reported(env):
    env.token_file.write_text("stale-service-token")
    r = await submit(env)
    assert r.status == 502


async def test_idle_clusters_are_deleted_and_busy_ones_kept(env):
    await env.client.post(
        "/api/jobs/", json={"entrypoint": "a"}, headers=auth("session-a")
    )
    await env.client.post(
        "/api/jobs/", json={"entrypoint": "b"}, headers=auth("session-b")
    )
    env.heads.jobs["ray-train-42"][0]["status"] = "SUCCEEDED"
    long_ago = time.monotonic() - gw.IDLE_TIMEOUT_S - 1
    env.gateway.last_used.update({"ray-train-7": long_ago, "ray-train-42": long_ago})

    await env.gateway.reap_idle()

    assert set(env.kube.clusters) == {"ray-train-7"}


async def test_a_recently_used_cluster_is_kept(env):
    await submit(env)
    env.heads.jobs["ray-train-7"][0]["status"] = "SUCCEEDED"
    await env.gateway.reap_idle()
    assert "ray-train-7" in env.kube.clusters


async def test_a_ready_head_that_does_not_answer_is_kept(env):
    await submit(env)
    env.kube.clusters["ray-train-7"]["status"] = {
        "conditions": [{"type": "HeadPodReady", "status": "True"}]
    }
    env.heads.up = False
    env.gateway.last_used["ray-train-7"] = time.monotonic() - gw.IDLE_TIMEOUT_S - 1

    await env.gateway.reap_idle()

    assert "ray-train-7" in env.kube.clusters


async def test_an_unreachable_head_that_never_came_up_is_deleted(env, monkeypatch):
    await submit(env)
    unreachable_heads(monkeypatch)
    env.gateway.last_used["ray-train-7"] = time.monotonic() - gw.IDLE_TIMEOUT_S - 1
    await env.gateway.reap_idle()
    assert env.kube.clusters == {}


async def test_a_cluster_made_under_another_service_token_is_deleted(env):
    await submit(env)
    env.token_file.write_text("rotated-service-token")
    env.gateway.last_used["ray-train-7"] = time.monotonic() - gw.IDLE_TIMEOUT_S - 1
    await env.gateway.reap_idle()
    assert env.kube.clusters == {}


async def test_reaping_waits_out_a_failed_listing(env):
    await submit(env)
    env.kube.fail_list = True
    env.gateway.last_used["ray-train-7"] = time.monotonic() - gw.IDLE_TIMEOUT_S - 1
    await env.gateway.reap_idle()
    assert "ray-train-7" in env.kube.clusters


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


async def test_the_app_starts_and_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "SERVICE_ACCOUNT", tmp_path)
    monkeypatch.setattr(
        gw,
        "ssl",
        SimpleNamespace(
            create_default_context=lambda cafile: ssl.create_default_context()
        ),
    )
    client = TestClient(TestServer(gw.make_app()))
    await client.start_server()
    assert (await client.get("/healthz")).status == 200
    assert isinstance(client.server.app[gw.GATEWAY], gw.Gateway)
    await client.close()


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
    with pytest.raises(web.HTTPForbidden):
        gw.ldap_ids("nobody")


def test_a_clusters_head_is_its_kuberay_service():
    assert (
        gw.head_url("ray-train-7")
        == "http://ray-train-7-head-svc.cms.svc.cluster.local:8265"
    )


def test_non_purdue_accounts_map_onto_the_pool():
    assert gw.ldap_account("user-a", 7) == "user-a"
    assert gw.ldap_account("user-b-cern", 42) == "paf0042"
    assert gw.ldap_account("user-c-fnal", 3) == "paf0003"
    with pytest.raises(web.HTTPForbidden):
        gw.ldap_account("user-d-cern", 400)


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
    user = gw.User(name="user-a", af_id=7, account="user-a", uid=5001, gid=500)
    gw.build_cluster(template, user)
    assert template == before


def test_each_cluster_has_its_own_token():
    assert gw.cluster_token("k", "ray-train-7") != gw.cluster_token("k", "ray-train-42")
    assert gw.cluster_token("k", "ray-train-7") != gw.cluster_token(
        "other", "ray-train-7"
    )
