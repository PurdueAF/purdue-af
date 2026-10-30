"""PurdueAF Agent: answers @mentions in one Mattermost channel, and direct
messages from that channel's members, from the user docs and the agentic
interface's facility-wide tools, as a Hub service that no user stands behind."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

log = logging.getLogger("mattermost-agent")

# The Hub's proxy serves the service here, and passes the whole path on.
PREFIX = "/services/mattermost-agent"
PORT = 8080
LOGIN_SECONDS = 600.0
MAX_LOGINS = 32
STALE_SECONDS = 180.0
POLL_SECONDS = 10.0
CHANNELS_SECONDS = 60.0
# Everything else on the MCP server acts on the caller's own session and data.
FACILITY_TOOLS = {
    "get_facility_health",
    "list_af_profiles",
    "list_dask_cluster_options",
}
MAX_TURNS = 6
MAX_THREAD_POSTS = 20
MAX_POST_CHARS = 4_000
MAX_TOOL_CHARS = 24_000
ANSWER_SECONDS = 300.0
CONCURRENT_ANSWERS = 4
USER_LIMIT = 5
USER_WINDOW_SECONDS = 600.0
# Refreshed this early, so that a poll never races the expiry.
EXPIRY_MARGIN_SECONDS = 300.0
DOC_TITLE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
# Before a name, so a reply can never ping @channel, @all or a person.
AT_NAME = re.compile(r"(?<![\w.])@(?=[\w.-])")
SORRY = (
    "I could not answer this just now. Please try again in a few minutes, or see "
    "https://analysis-facility.physics.purdue.edu/support/."
)
LIMITED = "You have reached my limit of questions for now. Please try again later."
NOT_MEMBER = "I answer direct messages from members of {channel} only."
NOT_ALL_MEMBERS = "I answer here only if everyone in this conversation is in {channel}."
NOT_READY = "I am not in {channel} yet, so I cannot answer here."


class Unauthorized(Exception):
    """Mattermost refused the tokens: the service account has to log in again."""


class OAuth(httpx.Auth):
    """Mattermost's OAuth tokens. Each refresh replaces the refresh token, so
    they live in a file that outlasts the pod. ``ready`` is set while it holds
    them; the service account's login at ``login_url`` provides them."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        state_file: Path,
        client_id: str,
        client_secret: str,
        username: str,
        public_url: str,
    ) -> None:
        self.client = client
        self.state_file = state_file
        self.client_id = client_id
        self.client_secret = client_secret
        self.username = username
        self.login_url = f"{public_url}{PREFIX}/"
        self.redirect = f"{public_url}{PREFIX}/callback"
        self.tokens: dict[str, Any] = {}
        self.logins: dict[str, float] = {}
        self.ready = asyncio.Event()
        self.lock = asyncio.Lock()
        if state_file.exists():
            self.tokens = json.loads(state_file.read_text())
            self.ready.set()

    def store(self, tokens: dict[str, Any]) -> None:
        self.tokens = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": time.time() + tokens["expires_in"],
        }
        temporary = self.state_file.with_suffix(".tmp")
        temporary.touch(mode=0o600)
        temporary.write_text(json.dumps(self.tokens))
        temporary.replace(self.state_file)

    def forget(self) -> None:
        self.tokens = {}
        self.state_file.unlink(missing_ok=True)
        self.ready.clear()

    def authorize_url(self, base_url: str) -> str:
        """Where a login starts, with a state that callback() accepts once."""
        now = time.monotonic()
        self.logins = {
            state: at for state, at in self.logins.items() if now - at < LOGIN_SECONDS
        }
        while len(self.logins) >= MAX_LOGINS:
            del self.logins[next(iter(self.logins))]
        state = secrets.token_urlsafe(24)
        self.logins[state] = now
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect,
                "state": state,
            }
        )
        return f"{base_url}/oauth/authorize?{query}"

    async def callback(self, state: str, code: str) -> bool:
        """Keeps the tokens of a login, only if it is the service account's."""
        at = self.logins.pop(state, None)
        if at is None or time.monotonic() - at >= LOGIN_SECONDS:
            return False
        response = await self.client.post(
            "/oauth/access_token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        if not response.is_success:
            log.warning("login: HTTP %s", response.status_code)
            return False
        tokens = response.json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        me = await self.client.get("/api/v4/users/me", headers=headers)
        me.raise_for_status()
        if me.json()["username"] != self.username:
            log.warning("login: not the service account")
            return False
        async with self.lock:
            self.store(tokens)
        self.ready.set()
        return True

    async def refresh(self) -> None:
        if not self.tokens:
            raise Unauthorized("no tokens")
        response = await self.client.post(
            "/oauth/access_token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.tokens["refresh_token"],
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        if response.status_code in (400, 401, 403, 404):
            raise Unauthorized(f"token refresh: HTTP {response.status_code}")
        response.raise_for_status()
        self.store(response.json())

    async def token(self, rejected: str = "") -> str:
        """A live access token, other than one Mattermost just rejected."""
        async with self.lock:
            current = self.tokens.get("access_token")
            expires_at = self.tokens.get("expires_at", 0.0)
            fresh = time.time() < expires_at - EXPIRY_MARGIN_SECONDS
            if not current or current == rejected or not fresh:
                await self.refresh()
            token: str = self.tokens["access_token"]
            return token

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self.token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            token = await self.token(rejected=token)
            request.headers["Authorization"] = f"Bearer {token}"
            yield request


class Docs:
    """The user docs, read on each use: kubelet updates them in place."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def pages(self) -> dict[str, Path]:
        return {path.stem: path for path in sorted(self.directory.glob("*.md"))}

    def index(self) -> str:
        lines = []
        for name, path in self.pages().items():
            title = DOC_TITLE.search(path.read_text())
            lines.append(f"- {name}: {title.group(1) if title else name}")
        return "\n".join(lines)

    def read(self, page: str) -> str:
        path = self.pages().get(page)
        return path.read_text() if path else f"Error: there is no page {page!r}."


class Mattermost:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def get(self, path: str, **params: Any) -> Any:
        response = await self.client.get(f"/api/v4{path}", params=params)
        response.raise_for_status()
        return response.json()

    async def channels(
        self, user_id: str, team_name: str, channel_name: str
    ) -> tuple[str | None, set[str], set[str]]:
        """The bot's public channel of that name, its direct messages and its
        group messages."""
        home, direct, groups = None, set(), set()
        for team in await self.get("/users/me/teams"):
            if team["display_name"] != team_name:
                continue
            for c in await self.get(f"/users/{user_id}/teams/{team['id']}/channels"):
                if c["type"] == "O" and c["display_name"] == channel_name:
                    home = c["id"]
                elif c["type"] == "D":
                    direct.add(c["id"])
                elif c["type"] == "G":
                    groups.add(c["id"])
        return home, direct, groups

    async def members(self, channel: str) -> list[str]:
        found = await self.get(f"/channels/{channel}/members", per_page=200)
        return [m["user_id"] for m in found]

    async def is_member(self, channel: str, user_id: str) -> bool:
        response = await self.client.get(
            f"/api/v4/channels/{channel}/members/{user_id}"
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def latest(self, channel: str) -> int:
        """Server time of the channel's newest post: where its polling starts."""
        page = await self.get(f"/channels/{channel}/posts", page=0, per_page=1)
        return max((p["update_at"] for p in page["posts"].values()), default=0)

    async def since(self, channel: str, cursor: int) -> list[dict[str, Any]]:
        page = await self.get(f"/channels/{channel}/posts", since=cursor)
        return sorted(page["posts"].values(), key=lambda p: p["create_at"])

    async def recent(self, channel: str, count: int) -> list[dict[str, Any]]:
        page = await self.get(f"/channels/{channel}/posts", page=0, per_page=count)
        return sorted(page["posts"].values(), key=lambda p: p["create_at"])

    async def thread(self, post_id: str) -> list[dict[str, Any]]:
        page = await self.get(f"/posts/{post_id}/thread")
        return sorted(page["posts"].values(), key=lambda p: p["create_at"])

    async def reply(self, post: dict[str, Any], root: str, message: str) -> None:
        response = await self.client.post(
            "/api/v4/posts",
            json={
                "channel_id": post["channel_id"],
                "root_id": root,
                "message": message,
            },
        )
        response.raise_for_status()


class Facility:
    """The agentic interface, in a fresh MCP session per question."""

    def __init__(self, url: str, token_file: Path) -> None:
        self.url = url
        self.token_file = token_file

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        token = self.token_file.read_text().strip()
        headers = {"Authorization": f"Bearer {token}"}
        async with create_mcp_http_client(headers=headers) as http:
            async with streamable_http_client(self.url, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session


def clean(text: str) -> str:
    """What may be posted: no mentions, and within a post's length."""
    return AT_NAME.sub("@​", text.strip())[:MAX_POST_CHARS]


def read_doc_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "read_doc",
            "description": "Read one page of the user documentation.",
            "parameters": {
                "type": "object",
                "properties": {"page": {"type": "string"}},
                "required": ["page"],
            },
        },
    }


def openai_tool(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema,
        },
    }


class Agent:
    def __init__(
        self,
        mattermost: Mattermost,
        llm: httpx.AsyncClient,
        facility: Facility,
        docs: Docs,
        prompt_file: Path,
        role: str,
        team_name: str,
        channel_name: str,
        oauth: OAuth,
    ) -> None:
        self.mattermost = mattermost
        self.llm = llm
        self.facility = facility
        self.docs = docs
        self.prompt_file = prompt_file
        self.role = role
        self.team_name = team_name
        self.channel_name = channel_name
        self.oauth = oauth
        self.beat = time.monotonic()
        self.me: dict[str, Any] = {}
        self.mention = re.compile("$^")
        self.cursors: dict[str, int] = {}
        self.home: str | None = None
        self.direct: set[str] = set()
        self.groups: set[str] = set()
        self.channels_at = 0.0
        self.asked: defaultdict[str, deque[float]] = defaultdict(deque)
        self.warned: set[str] = set()
        self.slots = asyncio.Semaphore(CONCURRENT_ANSWERS)
        self.tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self.me = await self.mattermost.get("/users/me")
        name = re.escape(self.me["username"])
        self.mention = re.compile(rf"(?<![\w.-])@{name}(?![\w-])", re.IGNORECASE)

    def wanted(self, post: dict[str, Any], cursor: int) -> bool:
        """A new post by a person: any in a direct message, one that mentions
        the bot elsewhere, group messages included. Posts of other bots are left out, so that two bots
        cannot keep answering each other."""
        props = post.get("props") or {}
        return (
            post["create_at"] > cursor
            and not post["delete_at"]
            and not post["type"]
            and post["user_id"] != self.me["id"]
            and str(props.get("from_bot", "")).lower() != "true"
            and (
                post["channel_id"] in self.direct
                or bool(self.mention.search(post["message"]))
            )
        )

    def allowed(self, user_id: str, now: float) -> bool:
        asked = self.asked[user_id]
        while asked and now - asked[0] > USER_WINDOW_SECONDS:
            asked.popleft()
        if len(asked) >= USER_LIMIT:
            return False
        asked.append(now)
        self.warned.discard(user_id)
        return True

    async def poll(self) -> list[asyncio.Task[None]]:
        now = time.monotonic()
        if not self.channels_at or now - self.channels_at > CHANNELS_SECONDS:
            first = not self.channels_at
            self.home, direct, groups = await self.mattermost.channels(
                self.me["id"], self.team_name, self.channel_name
            )
            if self.home is None:
                log.warning("not in %s", self.channel_name)
            if not first:
                # A conversation that appeared since the last look is all new.
                known = self.direct | self.groups
                self.cursors.update(dict.fromkeys((direct | groups) - known, 1))
            self.direct, self.groups = direct, groups
            self.channels_at = now
        started = []
        polled = [self.home] if self.home else []
        for channel in polled + sorted(self.direct | self.groups):
            cursor = self.cursors.get(channel)
            if cursor is None:
                self.cursors[channel] = await self.mattermost.latest(channel)
                continue
            posts = await self.mattermost.since(channel, cursor)
            for post in posts:
                self.cursors[channel] = max(self.cursors[channel], post["update_at"])
                if self.wanted(post, cursor):
                    task = asyncio.create_task(self.handle(post))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                    started.append(task)
        return started

    def private(self, post: dict[str, Any]) -> bool:
        return post["channel_id"] in self.direct | self.groups

    async def refusal(self, post: dict[str, Any]) -> str | None:
        """Why a private conversation gets no answer, if it does not."""
        if not self.private(post):
            return None
        if self.home is None:
            return NOT_READY
        if post["channel_id"] in self.direct:
            people, refused = [post["user_id"]], NOT_MEMBER
        else:
            people = await self.mattermost.members(post["channel_id"])
            refused = NOT_ALL_MEMBERS
        for person in people:
            if person != self.me["id"]:
                if not await self.mattermost.is_member(self.home, person):
                    return refused
        return None

    async def handle(self, post: dict[str, Any]) -> None:
        user = post["user_id"]
        root = post["root_id"] if self.private(post) else post["root_id"] or post["id"]
        try:
            refused = await self.refusal(post)
            if refused:
                notice = refused.format(channel=self.channel_name)
                await self.mattermost.reply(post, root, notice)
                log.info("post %s: refused", post["id"])
                return
            if not self.allowed(user, time.monotonic()):
                if user not in self.warned:
                    self.warned.add(user)
                    await self.mattermost.reply(post, root, LIMITED)
                log.info("post %s: over the limit", post["id"])
                return
            async with self.slots:
                try:
                    answer = await asyncio.wait_for(self.answer(post), ANSWER_SECONDS)
                except Exception:
                    log.exception("post %s: no answer", post["id"])
                    answer = SORRY
            await self.mattermost.reply(post, root, clean(answer) or SORRY)
            log.info("post %s: answered", post["id"])
        except (httpx.HTTPError, Unauthorized):
            log.exception("post %s: no reply", post["id"])

    def thread_messages(
        self, thread: list[dict[str, Any]], post: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """The conversation up to the question, without saying who wrote what."""
        earlier = [p for p in thread if p["create_at"] <= post["create_at"]]
        messages = []
        for p in earlier[-MAX_THREAD_POSTS:]:
            mine = p["user_id"] == self.me["id"]
            text = self.mention.sub("", p["message"]).strip()[:MAX_POST_CHARS]
            if text and not p["type"]:
                messages.append(
                    {"role": "assistant" if mine else "user", "content": text}
                )
        return messages

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], session: str
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.role, "messages": messages}
        if tools:
            body["tools"] = tools
        response = await self.llm.post(
            "/v1/chat/completions", json=body, headers={"x-af-session": session}
        )
        response.raise_for_status()
        message: dict[str, Any] = response.json()["choices"][0]["message"]
        return message

    async def call(self, session: ClientSession, name: str, arguments: str) -> str:
        """A tool's result, or what went wrong for the model to work with."""
        try:
            args = json.loads(arguments or "{}")
            if name == "read_doc":
                return self.docs.read(str(args.get("page")))[:MAX_TOOL_CHARS]
            if name not in FACILITY_TOOLS:
                return f"Error: there is no tool {name!r}."
            result = await session.call_tool(name, args)
            text = "\n".join(getattr(block, "text", "") for block in result.content)
            return text[:MAX_TOOL_CHARS]
        except Exception as exc:
            log.warning("tool %s failed: %s", name, type(exc).__name__)
            return f"Error: {name} failed."

    async def answer(self, post: dict[str, Any]) -> str:
        root = post["root_id"] or post["id"]
        if self.private(post):
            thread = await self.mattermost.recent(post["channel_id"], MAX_THREAD_POSTS)
        else:
            thread = await self.mattermost.thread(post["id"])
        system = f"{self.prompt_file.read_text()}\n\nPages:\n{self.docs.index()}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *self.thread_messages(thread, post),
        ]
        async with self.facility.session() as session:
            listed = await session.list_tools()
            tools = [read_doc_tool()] + [
                openai_tool(t) for t in listed.tools if t.name in FACILITY_TOOLS
            ]
            for turn in range(MAX_TURNS):
                last = turn == MAX_TURNS - 1
                reply = await self.complete(messages, [] if last else tools, root)
                calls = reply.get("tool_calls")
                if not calls:
                    return str(reply.get("content") or "")
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply.get("content"),
                        "tool_calls": calls,
                    }
                )
                for call in calls:
                    function = call["function"]
                    content = await self.call(
                        session, function["name"], function.get("arguments", "")
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": content}
                    )
        return ""

    def alive(self) -> bool:
        """Not stuck: polling recently, or waiting for the login."""
        recent = time.monotonic() - self.beat < STALE_SECONDS
        return recent or not self.oauth.ready.is_set()

    async def polling(self) -> None:
        await self.start()
        log.info("polling as %s", self.me["username"])
        while True:
            try:
                await self.poll()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    raise Unauthorized("HTTP 401 with fresh tokens") from exc
                log.warning("poll: HTTP %s", exc.response.status_code)
            except httpx.HTTPError as exc:
                log.warning("poll: %s", type(exc).__name__)
            self.beat = time.monotonic()
            await asyncio.sleep(POLL_SECONDS)

    async def run(self) -> None:
        while True:
            if not self.oauth.ready.is_set():
                log.warning("waiting for the login at %s", self.oauth.login_url)
            await self.oauth.ready.wait()
            try:
                await self.polling()
            except Unauthorized as exc:
                log.error("Mattermost refused the tokens: %s", exc)
                self.oauth.forget()


def web(agent: Agent, base_url: str) -> Starlette:
    """The login's two pages, the only ones anyone outside can reach, and
    the probe. No page shows a token, and only the service account's login
    is kept."""

    async def login(request: Request) -> Response:
        if agent.oauth.ready.is_set():
            return PlainTextResponse("Connected to Mattermost.")
        return RedirectResponse(agent.oauth.authorize_url(base_url))

    async def callback(request: Request) -> Response:
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        if await agent.oauth.callback(state, code):
            return PlainTextResponse("Connected to Mattermost.")
        return PlainTextResponse("This login was not accepted.", status_code=403)

    async def health(request: Request) -> Response:
        return PlainTextResponse("ok", status_code=200 if agent.alive() else 503)

    return Starlette(
        routes=[
            Route(f"{PREFIX}/", login),
            Route(f"{PREFIX}/callback", callback),
            Route("/healthz", health),
        ]
    )


async def serve(agent: Agent, base_url: str) -> None:
    config = uvicorn.Config(
        web(agent, base_url), host="0.0.0.0", port=PORT, log_level="warning"
    )
    await asyncio.gather(uvicorn.Server(config).serve(), agent.run())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # httpx logs every request at INFO, which is every poll.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    url = os.environ["MATTERMOST_URL"]
    oauth = OAuth(
        httpx.AsyncClient(base_url=url, timeout=30.0),
        Path(os.environ["STATE_FILE"]),
        os.environ["MATTERMOST_CLIENT_ID"],
        os.environ["MATTERMOST_CLIENT_SECRET"],
        os.environ["MATTERMOST_USERNAME"],
        os.environ["PUBLIC_URL"],
    )
    mattermost = httpx.AsyncClient(base_url=url, auth=oauth, timeout=30.0)
    llm = httpx.AsyncClient(
        base_url=os.environ["LLM_PROXY_URL"],
        headers={"Authorization": f"Bearer {os.environ['LLM_PROXY_TOKEN']}"},
        timeout=ANSWER_SECONDS,
    )
    agent = Agent(
        Mattermost(mattermost),
        llm,
        Facility(os.environ["MCP_URL"], Path(os.environ["HUB_TOKEN_FILE"])),
        Docs(Path(os.environ["DOCS_DIR"])),
        Path(os.environ["PROMPT_FILE"]),
        os.environ["LLM_ROLE"],
        os.environ["MATTERMOST_TEAM"],
        os.environ["MATTERMOST_CHANNEL"],
        oauth,
    )
    asyncio.run(serve(agent, url))


if __name__ == "__main__":
    main()
