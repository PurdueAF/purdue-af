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
        self.thread = {}
        self.replies = []
        self.since = []
        self.members = {"u1"}
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
        if path.startswith("/channels/c1/members/"):
            member = path.rsplit("/", 1)[1] in self.members
            return httpx.Response(200 if member else 404, json={})
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
        )
        await bot.start()
        return bot, server, llm, facility

    return build


async def poll(bot):
    await asyncio.gather(*await bot.poll())


async def test_a_mention_is_answered_in_its_thread_and_older_posts_are_not(make):
    bot, server, llm, _ = await make({"content": "Yes, it is up."})
    await poll(bot)
    assert bot.cursors == {"c1": 1000, "dm": 1000}
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
    assert found == ("c1", {"dm"})


async def test_a_member_is_answered_in_a_direct_message_without_a_mention(make):
    bot, server, llm, _ = await make({"content": "It is up."})
    await poll(bot)
    assert bot.cursors == {"c1": 1000, "dm": 1000}
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


async def test_a_new_direct_message_is_answered_from_its_first_post(make):
    bot, server, *_ = await make({"content": "Hello."})
    server.direct = []
    await poll(bot)
    assert bot.cursors == {"c1": 1000}
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
    assert "storage: Storage" in llm.bodies[0]["messages"][0]["content"]


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


async def test_a_rejected_token_stops_the_bot(make, monkeypatch):
    bot, *_ = await make()

    async def rejected():
        request = httpx.Request("GET", "http://mm")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(bot, "poll", rejected)
    with pytest.raises(httpx.HTTPStatusError):
        await bot.run()


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
    bot = agent.Agent(None, None, facility, None, None, "r", TEAM, CHANNEL)
    try:
        async with facility.session() as session:
            listed = await session.list_tools()
            assert [t.name for t in listed.tools] == ["get_facility_health"]
            assert await bot.call(session, "get_facility_health", "") == "**Healthy**"
    finally:
        live.should_exit = True
        thread.join(5)
    assert set(seen) == {b"Bearer hub-token"}


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
