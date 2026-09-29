"""Tests for apps/ray-train/gateway.py against fake JupyterHub, Kubernetes and Ray heads.

What the isolation rests on: a call is resolved to a user only by the Hub, it
reaches that user's cluster and no other, the cluster runs as the user's LDAP
account, and a head only ever sees its own derived token, never a session's.
"""

import copy
import time
from types import SimpleNamespace

import pytest
import yaml
from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from common import REPO, load_script

gw = load_script(REPO / "apps" / "ray-train" / "gateway.py", "ray_train_gateway")

TEMPLATE = REPO / "apps" / "ray-train" / "raycluster.yaml"
SERVICE_TOKEN = "service-token"
# session token -> (JupyterHub name, AF id)
SESSIONS = {
    "session-a": ("user-a", 7),
    "session-b": ("user-b-cern", 42),
}
LDAP = {"user-a": (5001, 500), "paf0042": (6042, 600)}


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def hub_app():
    async def whoami(request):
        _, _, token = request.headers.get("Authorization", "").partition(" ")
        if token not in SESSIONS:
            return web.json_response({}, status=403)
        return web.json_response({"name": SESSIONS[token][0]})

    async def user(request):
        if request.headers.get("Authorization") != f"token {SERVICE_TOKEN}":
            return web.json_response({}, status=403)
        name = request.match_info["name"]
        (af_id,) = [i for n, i in SESSIONS.values() if n == name]
        state = {"pod_name": f"purdue-af-{af_id}"}
        return web.json_response({"name": name, "servers": {"": {"state": state}}})

    app = web.Application()
    app.router.add_get("/hub/api/user", whoami)
    app.router.add_get("/hub/api/users/{name}", user)
    return app


class FakeKube:
    def __init__(self):
        self.clusters = {}
        self.secrets = {}

    def app(self):
        clusters = "/apis/ray.io/v1/namespaces/cms/rayclusters"

        async def create(request):
            body = await request.json()
            name = body["metadata"]["name"]
            if name in self.clusters:
                return web.json_response({"message": "exists"}, status=409)
            body["metadata"]["uid"] = f"uid-{name}"
            self.clusters[name] = body
            return web.json_response(body, status=201)

        async def listing(request):
            return web.json_response({"items": list(self.clusters.values())})

        async def get(request):
            cluster = self.clusters.get(request.match_info["name"])
            if cluster is None:
                return web.json_response({"message": "not found"}, status=404)
            return web.json_response(cluster)

        async def delete(request):
            self.clusters.pop(request.match_info["name"], None)
            return web.json_response({})

        async def secret(request):
            body = await request.json()
            name = body["metadata"]["name"]
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
        self.jobs = {}
        self.seen_tokens = []
        self.uploads = {}

    def app(self):
        def authorized(request):
            cluster = request.match_info["cluster"]
            token = request.headers.get("Authorization", "")
            self.seen_tokens.append(token)
            return token == f"Bearer {gw.cluster_token(SERVICE_TOKEN, cluster)}"

        @web.middleware
        async def check(request, handler):
            if not self.up:
                raise web.HTTPServiceUnavailable()
            if not authorized(request):
                raise web.HTTPForbidden()
            return await handler(request)

        async def version(request):
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
            await ws.send_str("epoch 1\n")
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
    monkeypatch.setattr(gw, "HUB_API", str(hub.make_url("/hub/api")))
    monkeypatch.setattr(gw, "KUBE_API", str(kube_server.make_url("")).rstrip("/"))
    monkeypatch.setattr(gw, "SERVICE_ACCOUNT", tmp_path)
    monkeypatch.setattr(gw, "TEMPLATE", TEMPLATE)
    monkeypatch.setattr(gw, "START_TIMEOUT_S", 5)
    monkeypatch.setattr(gw, "ldap_ids", lambda account: LDAP[account])
    heads_url = str(head_server.make_url("")).rstrip("/")
    monkeypatch.setattr(gw, "head_url", lambda cluster: f"{heads_url}/{cluster}")

    async with ClientSession() as http, ClientSession() as kube_http:
        gateway = gw.Gateway(http, kube_http, SERVICE_TOKEN)
        app = web.Application()
        app[gw.GATEWAY] = gateway
        app.router.add_route("*", "/{path:.*}", gw.handle)
        client = TestClient(TestServer(app))
        await client.start_server()
        yield SimpleNamespace(client=client, gateway=gateway, kube=kube, heads=heads)
        await client.close()
    for server in servers:
        await server.close()


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
    env.gateway.service_token = ""
    r = await env.client.get("/api/version", headers=auth("session-a"))
    assert r.status == 503


async def test_version_and_listing_start_no_cluster(env):
    r = await env.client.get("/api/version", headers=auth("session-a"))
    assert (await r.json())["ray_version"] == yaml.safe_load(TEMPLATE.read_text())[
        "spec"
    ]["rayVersion"]
    r = await env.client.get("/api/jobs/", headers=auth("session-a"))
    assert await r.json() == []
    assert env.kube.clusters == {}


async def test_submitting_starts_the_users_cluster_as_them(env):
    r = await env.client.post(
        "/api/jobs/", json={"entrypoint": "python train.py"}, headers=auth("session-a")
    )
    assert r.status == 200
    assert (await r.json())["job_id"] == "raysubmit_ray-train-7"

    cluster = env.kube.clusters["ray-train-7"]
    pod = cluster["spec"]["headGroupSpec"]["template"]["spec"]
    assert pod["securityContext"] == {"runAsUser": 5001, "runAsGroup": 500}
    assert {"name": "USER", "value": "user-a"} in pod["containers"][0]["env"]
    assert cluster["spec"]["authOptions"]["secretName"] == "ray-train-7"

    secret = env.kube.secrets["ray-train-7"]
    assert secret["stringData"]["auth_token"] == gw.cluster_token(
        SERVICE_TOKEN, "ray-train-7"
    )
    assert secret["metadata"]["ownerReferences"][0]["uid"] == "uid-ray-train-7"


async def test_a_head_never_sees_a_session_token(env):
    await env.client.post(
        "/api/jobs/", json={"entrypoint": "true"}, headers=auth("session-a")
    )
    await env.client.get("/api/jobs/", headers=auth("session-a"))
    assert env.heads.seen_tokens
    assert not any("session-a" in token for token in env.heads.seen_tokens)


async def test_an_external_user_runs_as_their_pooled_account(env):
    await env.client.post(
        "/api/jobs/", json={"entrypoint": "true"}, headers=auth("session-b")
    )
    pod = env.kube.clusters["ray-train-42"]["spec"]["headGroupSpec"]["template"]["spec"]
    assert pod["securityContext"] == {"runAsUser": 6042, "runAsGroup": 600}


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
    await env.client.post(
        "/api/jobs/", json={"entrypoint": "true"}, headers=auth("session-a")
    )
    ws = await env.client.ws_connect(
        "/api/jobs/raysubmit_ray-train-7/logs/tail", headers=auth("session-a")
    )
    lines = [message.data async for message in ws if message.type == WSMsgType.TEXT]
    assert lines == ["epoch 0\n", "epoch 1\n"]


async def test_a_cluster_that_does_not_start_is_reported(env, monkeypatch):
    env.heads.up = False
    monkeypatch.setattr(gw, "START_TIMEOUT_S", 0)
    r = await env.client.post(
        "/api/jobs/", json={"entrypoint": "true"}, headers=auth("session-a")
    )
    assert r.status == 503


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


async def test_a_ready_head_that_does_not_answer_is_kept(env):
    await env.client.post(
        "/api/jobs/", json={"entrypoint": "a"}, headers=auth("session-a")
    )
    env.kube.clusters["ray-train-7"]["status"] = {
        "conditions": [{"type": "HeadPodReady", "status": "True"}]
    }
    env.heads.up = False
    env.gateway.last_used["ray-train-7"] = time.monotonic() - gw.IDLE_TIMEOUT_S - 1

    await env.gateway.reap_idle()

    assert "ray-train-7" in env.kube.clusters


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
