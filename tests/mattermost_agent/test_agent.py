"""apps/mattermost-agent/agent.py against fake Mattermost, LLM proxy and MCP
server, and its wiring to the app's manifests."""

import asyncio
import json
import threading
import time

import httpx
import pytest
import uvicorn
import yaml
from common import REPO, load_script
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

APP = REPO / "apps" / "mattermost-agent"
EXPERIMENTAL = REPO / "flux" / "experimental" / "kustomization.yaml"
agent = load_script(APP / "agent.py", "mattermost_agent")
BOT = {"id": "bot", "username": "purdueaf-agent"}
TEAM, CHANNEL = "CMS Experiment", "Purdue Analysis Facility"
PUBLIC = "https://af.example"


def post(id="p1", message="@purdueaf-agent is the AF up?", at=2000, **fields):
    return {
        "id": id,
        "channel_id": "c1",
        "root_id": "",
        "user_id": "u1",
        "create_at": at,
        "update_at": at,
        "delete_at": 0,
        "type": "",
        "message": message,
        "props": {},
        **fields,
    }


class FakeMattermost:
    def __init__(self):
        self.posts = {}
        self.direct_posts = {}
        self.group_posts = {}
        self.group = ["bot", "u1", "u2"]
        self.thread = {}
        self.replies = []
        self.since = []
        self.members = {"u1", "u2"}
        self.direct = [{"id": "dm", "type": "D", "display_name": ""}]

    def __call__(self, request):
        path = request.url.path.removeprefix("/api/v4")
        if request.method == "POST":
            self.replies.append(json.loads(request.content))
            return httpx.Response(201, json={})
        if path == "/users/me":
            return httpx.Response(200, json=BOT)
        if path == "/users/me/teams":
            teams = [
                {"id": "t1", "display_name": TEAM},
                {"id": "t2", "display_name": "Another Team"},
            ]
            return httpx.Response(200, json=teams)
        if path == "/users/bot/teams/t1/channels":
            channels = [
                {"id": "c1", "type": "O", "display_name": CHANNEL},
                {"id": "c2", "type": "O", "display_name": "Town Square"},
                {"id": "c3", "type": "P", "display_name": CHANNEL},
                {"id": "g1", "type": "G", "display_name": ""},
                *self.direct,
            ]
            return httpx.Response(200, json=channels)
        if path == "/users/bot/teams/t2/channels":
            other = [{"id": "c9", "type": "O", "display_name": CHANNEL}]
            return httpx.Response(200, json=other)
        if path == "/channels/g1/members":
            members = [{"user_id": user} for user in self.group]
            return httpx.Response(200, json=members)
        if path.startswith("/channels/c1/members/"):
            member = path.rsplit("/", 1)[1] in self.members
            return httpx.Response(200 if member else 404, json={})
        if path == "/channels/g1/posts":
            if "since" in request.url.params or self.group_posts:
                return httpx.Response(200, json={"posts": self.group_posts})
            return httpx.Response(200, json={"posts": {"old": post("old", at=1000)}})
        if path in ("/channels/c1/posts", "/channels/dm/posts"):
            channel = path.split("/")[2]
            if "since" in request.url.params:
                if channel == "dm":
                    return httpx.Response(200, json={"posts": self.direct_posts})
                self.since.append(int(request.url.params["since"]))
                return httpx.Response(200, json={"posts": self.posts})
            if "per_page" in request.url.params and self.direct_posts:
                return httpx.Response(200, json={"posts": self.direct_posts})
            return httpx.Response(200, json={"posts": {"old": post("old", at=1000)}})
        if path.startswith("/posts/"):
            return httpx.Response(200, json={"posts": self.thread})
        return httpx.Response(404)


class FakeLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.bodies = []

    def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        reply = self.replies.pop(0)
        if isinstance(reply, int):
            return httpx.Response(reply)
        return httpx.Response(200, json={"choices": [{"message": reply}]})


class FakeSession:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        class Tool:
            description = "a tool"
            input_schema = {"type": "object", "properties": {}}

            def __init__(self, name):
                self.name = name

        class Listed:
            tools = [Tool("get_facility_health"), Tool("stop_af_session")]

        return Listed()

    async def call_tool(self, name, args):
        self.calls.append(name)

        class Block:
            text = "**Healthy**"

        class Result:
            content = [Block()]

        return Result()


class FakeFacility:
    def __init__(self):
        self.mcp = FakeSession()

    def session(self):
        fake = self.mcp

        class Context:
            async def __aenter__(self):
                return fake

            async def __aexit__(self, *exc):
                return False

        return Context()


class FakeAuth:
    def __init__(self):
        self.ready = asyncio.Event()
        self.ready.set()


def tool_call(name, arguments="{}", id="call-1"):
    function = {"name": name, "arguments": arguments}
    return {"tool_calls": [{"id": id, "type": "function", "function": function}]}


@pytest.fixture
def docs(tmp_path):
    directory = tmp_path / "docs"
    directory.mkdir()
    (directory / "storage.md").write_text("# Storage\n\nHome is backed up.\n")
    return directory


@pytest.fixture
def make(tmp_path, docs):
    created = []

    async def build(*replies):
        prompt = tmp_path / "prompt.md"
        prompt.write_text("You are the agent.")
        server = FakeMattermost()
        llm = FakeLLM(*replies)
        clients = [
            httpx.AsyncClient(
                base_url="http://mm", transport=httpx.MockTransport(server)
            ),
            httpx.AsyncClient(
                base_url="http://llm", transport=httpx.MockTransport(llm)
            ),
        ]
        created.extend(clients)
        facility = FakeFacility()
        bot = agent.Agent(
            agent.Mattermost(clients[0]),
            clients[1],
            facility,
            agent.Docs(docs),
            prompt,
            "facility-assistant",
            TEAM,
            CHANNEL,
            FakeAuth(),
        )
        await bot.start()
        return bot, server, llm, facility

    return build


async def poll(bot):
    await asyncio.gather(*await bot.poll())


async def test_a_mention_is_answered_in_its_thread_and_older_posts_are_not(make):
    bot, server, llm, _ = await make({"content": "Yes, it is up."})
    await poll(bot)
    assert bot.cursors == {"c1": 1000, "dm": 1000, "g1": 1000}
    assert server.replies == []
    server.posts = {"p1": post()}
    server.thread = {"p1": post()}
    await poll(bot)
    assert server.since == [1000]
    assert server.replies == [
        {"channel_id": "c1", "root_id": "p1", "message": "Yes, it is up."}
    ]
    assert llm.bodies[0]["messages"][1] == {"role": "user", "content": "is the AF up?"}
    await poll(bot)
    assert len(server.replies) == 1


@pytest.mark.parametrize(
    "unwanted",
    [
        post(message="is the AF up?"),
        post(message="mail @purdueaf-agent-two"),
        post(user_id="bot"),
        post(props={"from_bot": "true"}),
        post(delete_at=3000),
        post(type="system_add_to_channel"),
        post(at=900, update_at=2000),
    ],
)
async def test_only_a_new_post_by_a_person_that_mentions_the_bot_is_wanted(
    make, unwanted
):
    bot, *_ = await make()
    assert not bot.wanted(unwanted, 1000)


async def test_only_the_named_public_channel_and_direct_messages_are_polled(make):
    bot, *_ = await make()
    found = await bot.mattermost.channels("bot", TEAM, CHANNEL)
    assert found == ("c1", {"dm"}, {"g1"})


async def test_a_member_is_answered_in_a_direct_message_without_a_mention(make):
    bot, server, llm, _ = await make({"content": "It is up."})
    await poll(bot)
    assert bot.cursors == {"c1": 1000, "dm": 1000, "g1": 1000}
    question = post("d1", "is the AF up?", channel_id="dm")
    server.direct_posts = {
        "d0": post("d0", "Earlier.", 1500, channel_id="dm", user_id="bot"),
        "d1": question,
    }
    await poll(bot)
    assert server.replies == [
        {"channel_id": "dm", "root_id": "", "message": "It is up."}
    ]
    assert llm.bodies[0]["messages"][1:] == [
        {"role": "assistant", "content": "Earlier."},
        {"role": "user", "content": "is the AF up?"},
    ]


def group_post(id="g", message="@purdueaf-agent is the AF up?", **fields):
    return post(id, message, channel_id="g1", **fields)


async def test_a_mention_in_a_group_of_members_is_answered(make):
    bot, server, llm, _ = await make({"content": "It is up."})
    await poll(bot)
    server.group_posts = {
        "g": group_post(),
        "h": group_post("h", "no mention", at=2100),
    }
    await poll(bot)
    assert server.replies == [
        {"channel_id": "g1", "root_id": "", "message": "It is up."}
    ]
    assert llm.bodies[0]["messages"][1] == {"role": "user", "content": "is the AF up?"}


async def test_a_group_with_anyone_outside_the_channel_gets_no_answer(make):
    bot, server, llm, _ = await make({"content": "never"})
    await poll(bot)
    server.members = {"u1"}
    server.group_posts = {"g": group_post()}
    await poll(bot)
    assert llm.bodies == []
    expected = agent.NOT_ALL_MEMBERS.format(channel=CHANNEL)
    assert [r["message"] for r in server.replies] == [expected]


async def test_a_group_message_without_a_mention_is_left_alone(make):
    bot, server, llm, _ = await make({"content": "never"})
    await poll(bot)
    server.group_posts = {"g": group_post(message="is the AF up?")}
    await poll(bot)
    assert server.replies == []


async def test_private_conversations_say_so_while_the_bot_is_outside_the_channel(make):
    bot, server, llm, _ = await make({"content": "never"})
    await poll(bot)
    bot.home = None
    await bot.handle(post("d1", "hi", channel_id="dm"))
    await bot.handle(group_post())
    expected = agent.NOT_READY.format(channel=CHANNEL)
    assert [r["message"] for r in server.replies] == [expected, expected]
    assert llm.bodies == []


async def test_a_new_direct_message_is_answered_from_its_first_post(make):
    bot, server, *_ = await make({"content": "Hello."})
    server.direct = []
    await poll(bot)
    assert bot.cursors == {"c1": 1000, "g1": 1000}
    server.direct = [{"id": "dm", "type": "D", "display_name": ""}]
    server.direct_posts = {"d1": post("d1", "hi", channel_id="dm")}
    bot.channels_at -= agent.CHANNELS_SECONDS + 1
    await poll(bot)
    assert [r["message"] for r in server.replies] == ["Hello."]


async def test_a_direct_message_from_a_non_member_gets_no_answer(make):
    bot, server, llm, _ = await make({"content": "never"})
    server.members = set()
    await poll(bot)
    server.direct_posts = {"d1": post("d1", "is the AF up?", channel_id="dm")}
    await poll(bot)
    assert llm.bodies == []
    expected = agent.NOT_MEMBER.format(channel=CHANNEL)
    assert [r["message"] for r in server.replies] == [expected]


async def test_tools_are_limited_to_the_facility_ones_and_docs(make):
    bot, server, llm, facility = await make(
        tool_call("get_facility_health"),
        tool_call("stop_af_session", id="call-2"),
        tool_call("read_doc", '{"page": "storage"}', id="call-3"),
        {"content": "Healthy, and home is backed up."},
    )
    server.posts = server.thread = {"p1": post()}
    bot.cursors["c1"] = 1000
    await poll(bot)
    offered = [t["function"]["name"] for t in llm.bodies[0]["tools"]]
    assert offered == ["read_doc", "get_facility_health"]
    assert facility.mcp.calls == ["get_facility_health"]
    results = [m["content"] for m in llm.bodies[-1]["messages"] if m["role"] == "tool"]
    assert results[0] == "**Healthy**"
    assert "no tool" in results[1]
    assert "Home is backed up." in results[2]
    system = llm.bodies[0]["messages"][0]["content"]
    assert "storage: Storage" in system
    assert "This conversation is in the channel." in system


async def test_the_last_turn_gets_no_tools_so_that_it_must_answer(make):
    bot, server, llm, _ = await make(
        *[tool_call("get_facility_health")] * (agent.MAX_TURNS - 1),
        {"content": "Done."},
    )
    server.posts = server.thread = {"p1": post()}
    bot.cursors["c1"] = 1000
    await poll(bot)
    assert "tools" not in llm.bodies[-1]
    assert server.replies[0]["message"] == "Done."


async def test_a_reply_cannot_mention_anyone(make):
    assert agent.clean("ping @channel, @all and @some.one") == (
        "ping @​channel, @​all and @​some.one"
    )
    assert agent.clean("write to a@cern.ch") == "write to a@cern.ch"
    assert len(agent.clean("x" * 10_000)) == agent.MAX_POST_CHARS


async def test_the_thread_reaches_the_model_without_who_wrote_it(make):
    bot, server, llm, _ = await make({"content": "It is."})
    server.posts = {"p3": post("p3", "@purdueaf-agent and now?", 3000, root_id="p1")}
    server.thread = {
        "p1": post("p1", "@purdueaf-agent up?", 1500),
        "p2": post("p2", "Yes.", 1600, user_id="bot"),
        **server.posts,
    }
    bot.cursors["c1"] = 2000
    await poll(bot)
    assert llm.bodies[0]["messages"][1:] == [
        {"role": "user", "content": "up?"},
        {"role": "assistant", "content": "Yes."},
        {"role": "user", "content": "and now?"},
    ]
    assert server.replies[0]["root_id"] == "p1"


async def test_a_failed_answer_says_so(make):
    bot, server, *_ = await make(500)
    server.posts = server.thread = {"p1": post()}
    bot.cursors["c1"] = 1000
    await poll(bot)
    assert server.replies[0]["message"] == agent.SORRY


async def test_a_user_over_the_limit_is_told_once(make):
    bot, server, *_ = await make(*[{"content": "ok"}] * agent.USER_LIMIT)
    for n in range(agent.USER_LIMIT + 2):
        await bot.handle(post(f"p{n}"))
    messages = [r["message"] for r in server.replies]
    assert messages == ["ok"] * agent.USER_LIMIT + [agent.LIMITED]


def test_the_limit_frees_up_with_time(make):
    bot = agent.Agent.__new__(agent.Agent)
    bot.asked, bot.warned = agent.defaultdict(agent.deque), set()
    assert all(bot.allowed("u", 0.0) for _ in range(agent.USER_LIMIT))
    assert not bot.allowed("u", 1.0)
    assert bot.allowed("u", agent.USER_WINDOW_SECONDS + 1)


async def test_a_session_against_a_real_mcp_server_sends_the_hub_token(tmp_path):
    seen = []
    server = MCPServer("fake")

    @server.tool()
    def get_facility_health() -> str:
        return "**Healthy**"

    app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    )

    async def recording(scope, receive, send):
        if scope["type"] == "http":
            seen.append(dict(scope["headers"]).get(b"authorization"))
        await app(scope, receive, send)

    live = uvicorn.Server(
        uvicorn.Config(recording, port=0, log_level="error", lifespan="on")
    )
    thread = threading.Thread(target=live.run, daemon=True)
    thread.start()
    while not live.started:
        time.sleep(0.01)
    port = live.servers[0].sockets[0].getsockname()[1]
    token = tmp_path / "token"
    token.write_text("hub-token\n")
    facility = agent.Facility(f"http://127.0.0.1:{port}/mcp", token)
    bot = agent.Agent(None, None, facility, None, None, "r", TEAM, CHANNEL, None)
    try:
        async with facility.session() as session:
            listed = await session.list_tools()
            assert [t.name for t in listed.tools] == ["get_facility_health"]
            assert await bot.call(session, "get_facility_health", "") == "**Healthy**"
    finally:
        live.should_exit = True
        thread.join(5)
    assert set(seen) == {b"Bearer hub-token"}


class FakeOAuthServer:
    """Replaces the refresh token on each use, as Mattermost does."""

    def __init__(self, expires_in=3600):
        self.expires_in = expires_in
        self.issued = 0
        self.valid = None
        self.login = "agent-account"
        self.rejected = set()
        self.seen = []

    def issue(self):
        self.issued += 1
        self.valid = f"refresh-{self.issued}"
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{self.issued}",
                "refresh_token": self.valid,
                "expires_in": self.expires_in,
            },
        )

    def __call__(self, request):
        if request.url.path == "/oauth/access_token":
            form = dict(httpx.QueryParams(request.content.decode()))
            assert (form["client_id"], form["client_secret"]) == ("id", "secret")
            if form["grant_type"] == "authorization_code":
                assert form["redirect_uri"] == f"{PUBLIC}{agent.PREFIX}/callback"
                return self.issue() if form["code"] == "good" else httpx.Response(400)
            assert form["grant_type"] == "refresh_token"
            if form["refresh_token"] != self.valid:
                return httpx.Response(404)
            return self.issue()
        token = request.headers["Authorization"].removeprefix("Bearer ")
        if request.url.path == "/api/v4/users/me":
            return httpx.Response(200, json={"username": self.login})
        self.seen.append(token)
        return httpx.Response(401 if token in self.rejected else 200, json={})


def oauth(server, state_file):
    transport = httpx.MockTransport(server)
    plain = httpx.AsyncClient(base_url="http://mm", transport=transport)
    return agent.OAuth(plain, state_file, "id", "secret", "agent-account", PUBLIC)


def oauth_client(server, state_file):
    auth = oauth(server, state_file)
    transport = httpx.MockTransport(server)
    return httpx.AsyncClient(base_url="http://mm", transport=transport, auth=auth)


def state_of(url):
    return httpx.URL(url).params["state"]


async def logged_in(server, state_file):
    client = oauth_client(server, state_file)
    state = state_of(client.auth.authorize_url("http://mm"))
    assert await client.auth.callback(state, "good")
    return client


async def test_the_service_accounts_login_is_kept(tmp_path):
    server, state = FakeOAuthServer(), tmp_path / "oauth.json"
    client = await logged_in(server, state)
    assert client.auth.ready.is_set()
    assert state.stat().st_mode & 0o777 == 0o600
    await client.get("/x")
    assert server.seen == ["access-1"]


async def test_a_login_as_anyone_else_is_not_kept(tmp_path):
    server, state = FakeOAuthServer(), tmp_path / "oauth.json"
    server.login = "someone-else"
    auth = oauth(server, state)
    url = auth.authorize_url("http://mm")
    assert not await auth.callback(state_of(url), "good")
    assert not auth.ready.is_set()
    assert not state.exists()


async def test_a_forged_reused_or_old_state_is_refused(tmp_path, monkeypatch):
    server = FakeOAuthServer()
    auth = oauth(server, tmp_path / "oauth.json")
    assert not await auth.callback("forged", "good")
    state = state_of(auth.authorize_url("http://mm"))
    assert await auth.callback(state, "good")
    assert not await auth.callback(state, "good")
    state = state_of(auth.authorize_url("http://mm"))
    now = time.monotonic()
    monkeypatch.setattr(agent.time, "monotonic", lambda: now + agent.LOGIN_SECONDS + 1)
    assert not await auth.callback(state, "good")


async def test_a_failed_code_exchange_is_refused(tmp_path):
    auth = oauth(FakeOAuthServer(), tmp_path / "oauth.json")
    assert not await auth.callback(state_of(auth.authorize_url("http://mm")), "bad")


async def test_pending_logins_are_bounded(tmp_path):
    auth = oauth(FakeOAuthServer(), tmp_path / "oauth.json")
    for _ in range(agent.MAX_LOGINS * 2):
        auth.authorize_url("http://mm")
    assert len(auth.logins) == agent.MAX_LOGINS


async def test_oauth_refreshes_once_for_concurrent_requests(tmp_path):
    server = FakeOAuthServer(expires_in=0)
    client = await logged_in(server, tmp_path / "oauth.json")
    server.expires_in = 3600
    await asyncio.gather(*[client.get("/x") for _ in range(4)])
    assert server.issued == 2
    assert server.seen == ["access-2"] * 4


async def test_oauth_tokens_survive_a_restart(tmp_path):
    server, state = FakeOAuthServer(), tmp_path / "oauth.json"
    await logged_in(server, state)
    client = oauth_client(server, state)
    assert client.auth.ready.is_set()
    await client.get("/x")
    assert server.issued == 1
    assert server.seen == ["access-1"]


async def test_oauth_uses_the_rotated_token_after_a_restart(tmp_path):
    server, state = FakeOAuthServer(expires_in=0), tmp_path / "oauth.json"
    await (await logged_in(server, state)).get("/x")
    await oauth_client(server, state).get("/x")
    assert server.seen == ["access-2", "access-3"]


async def test_a_rejected_access_token_is_refreshed_and_retried_once(tmp_path):
    server = FakeOAuthServer()
    client = await logged_in(server, tmp_path / "oauth.json")
    await client.get("/x")
    server.rejected = {"access-1"}
    response = await client.get("/x")
    assert response.status_code == 200
    assert server.seen == ["access-1", "access-1", "access-2"]


async def test_a_network_failure_keeps_the_refresh_token(tmp_path):
    server, state = FakeOAuthServer(expires_in=0), tmp_path / "oauth.json"
    client = await logged_in(server, state)

    def down(request):
        raise httpx.ConnectError("down")

    client.auth.client = httpx.AsyncClient(
        base_url="http://mm", transport=httpx.MockTransport(down)
    )
    with pytest.raises(httpx.ConnectError):
        await client.get("/x")
    assert client.auth.tokens["refresh_token"] == "refresh-1"


async def test_refused_tokens_send_the_agent_back_to_waiting_for_a_login(
    make, tmp_path
):
    bot, *_ = await make()
    server, state = FakeOAuthServer(), tmp_path / "oauth.json"
    client = await logged_in(server, state)
    client.auth.tokens["expires_at"] = 0
    server.valid = "revoked"
    bot.oauth = client.auth
    bot.mattermost = agent.Mattermost(client)
    running = asyncio.create_task(bot.run())
    while client.auth.ready.is_set():
        await asyncio.sleep(0)
    assert not state.exists()
    assert not running.done()
    assert bot.alive()
    running.cancel()


async def test_the_pages_start_a_login_and_finish_it(make, tmp_path):
    bot, *_ = await make()
    bot.oauth = oauth(FakeOAuthServer(), tmp_path / "oauth.json")
    transport = httpx.ASGITransport(app=agent.web(bot, "http://mm"))
    client = httpx.AsyncClient(base_url=PUBLIC, transport=transport)
    page = await client.get(f"{agent.PREFIX}/")
    assert page.status_code == 307
    assert page.headers["location"].startswith("http://mm/oauth/authorize?")
    query = {"state": "forged", "code": "good"}
    refused = await client.get(f"{agent.PREFIX}/callback", params=query)
    assert refused.status_code == 403
    query["state"] = state_of(page.headers["location"])
    done = await client.get(f"{agent.PREFIX}/callback", params=query)
    assert done.status_code == 200
    assert "refresh-" not in done.text and "access-" not in done.text
    assert (await client.get(f"{agent.PREFIX}/")).status_code == 200


async def test_the_probe_fails_only_when_polling_has_stalled(make, tmp_path):
    bot, *_ = await make()
    transport = httpx.ASGITransport(app=agent.web(bot, "http://mm"))
    client = httpx.AsyncClient(base_url=PUBLIC, transport=transport)
    assert (await client.get("/healthz")).status_code == 200
    bot.beat -= agent.STALE_SECONDS + 1
    assert (await client.get("/healthz")).status_code == 503
    bot.oauth.ready.clear()
    assert (await client.get("/healthz")).status_code == 200


def test_every_key_the_pod_reads_is_in_the_secret():
    deployment = yaml.safe_load((APP / "deployment.yaml").read_text())
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    keys = {
        e["valueFrom"]["secretKeyRef"]["key"]
        for e in container["env"]
        if e.get("valueFrom", {}).get("secretKeyRef", {}).get("name")
        == "mattermost-agent"
    }
    secret = yaml.safe_load((APP / "secret.yaml").read_text())
    assert keys == set(secret["stringData"])


def test_every_user_doc_is_mounted():
    kustomization = yaml.safe_load(EXPERIMENTAL.read_text())
    generated = {g["name"]: g for g in kustomization["configMapGenerator"]}
    files = generated["mattermost-agent-docs"]["files"]
    pages = {f.rsplit("/", 1)[1] for f in files}
    assert pages == {p.name for p in (REPO / "docs" / "docs").glob("*.md")} - {
        "contributing.md"
    }


def test_its_role_is_served_by_the_llm_proxy():
    deployment = yaml.safe_load((APP / "deployment.yaml").read_text())
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    llm = load_script(REPO / "apps/agentic-analysis/llm_proxy.py", "llm_proxy_config")
    config = llm.load(REPO / "apps/agentic-analysis/models.toml")
    assert env["LLM_ROLE"] in config.roles
