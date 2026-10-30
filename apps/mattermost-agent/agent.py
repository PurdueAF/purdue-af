"""PurdueAF Agent: answers @mentions in one Mattermost channel, and direct
messages from that channel's members, from the user docs and the agentic
interface's facility-wide tools, as a Hub service that no user stands behind."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

log = logging.getLogger("mattermost-agent")

HEARTBEAT = Path("/tmp/heartbeat")
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


class Unauthorized(Exception):
    """Mattermost refused the refresh token: authorize.py has to run again."""


class OAuth(httpx.Auth):
    """Mattermost's OAuth tokens. Each refresh replaces the refresh token, so
    they live in a file that outlasts the pod; a new seed from authorize.py,
    in the Secret, takes over from what the file holds."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        state_file: Path,
        client_id: str,
        client_secret: str,
        seed: str,
    ) -> None:
        self.client = client
        self.state_file = state_file
        self.client_id = client_id
        self.client_secret = client_secret
        self.seed = hashlib.sha256(seed.encode()).hexdigest()
        self.state = {"seed": self.seed, "refresh_token": seed, "access_token": ""}
        self.expires_at = 0.0
        self.lock = asyncio.Lock()
        if state_file.exists():
            saved = json.loads(state_file.read_text())
            if saved["seed"] == self.seed:
                self.state = saved
                self.expires_at = saved["expires_at"]

    def save(self) -> None:
        temporary = self.state_file.with_suffix(".tmp")
        temporary.touch(mode=0o600)
        temporary.write_text(json.dumps(self.state | {"expires_at": self.expires_at}))
        temporary.replace(self.state_file)

    async def refresh(self) -> None:
        response = await self.client.post(
            "/oauth/access_token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.state["refresh_token"],
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        if response.status_code in (400, 401, 403, 404):
            raise Unauthorized(f"token refresh: HTTP {response.status_code}")
        response.raise_for_status()
        tokens = response.json()
        self.state["refresh_token"] = tokens["refresh_token"]
        self.state["access_token"] = tokens["access_token"]
        self.expires_at = time.time() + tokens["expires_in"]
        self.save()

    async def token(self, rejected: str = "") -> str:
        """A live access token, other than one Mattermost just rejected."""
        async with self.lock:
            current = self.state["access_token"]
            fresh = time.time() < self.expires_at - EXPIRY_MARGIN_SECONDS
            if not current or current == rejected or not fresh:
                await self.refresh()
            return self.state["access_token"]

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
    ) -> tuple[str | None, set[str]]:
        """The bot's public channel of that name, and its direct messages."""
        home, direct = None, set()
        for team in await self.get("/users/me/teams"):
            if team["display_name"] != team_name:
                continue
            for c in await self.get(f"/users/{user_id}/teams/{team['id']}/channels"):
                if c["type"] == "O" and c["display_name"] == channel_name:
                    home = c["id"]
                elif c["type"] == "D":
                    direct.add(c["id"])
        return home, direct

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
    ) -> None:
        self.mattermost = mattermost
        self.llm = llm
        self.facility = facility
        self.docs = docs
        self.prompt_file = prompt_file
        self.role = role
        self.team_name = team_name
        self.channel_name = channel_name
        self.me: dict[str, Any] = {}
        self.mention = re.compile("$^")
        self.cursors: dict[str, int] = {}
        self.home: str | None = None
        self.direct: set[str] = set()
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
        the bot elsewhere. Posts of other bots are left out, so that two bots
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
            self.home, direct = await self.mattermost.channels(
                self.me["id"], self.team_name, self.channel_name
            )
            if self.home is None:
                log.warning("not in %s", self.channel_name)
            if not first:
                # A direct message that appeared since the last look is all new.
                self.cursors.update(dict.fromkeys(direct - self.direct, 1))
            self.direct = direct
            self.channels_at = now
        started = []
        polled = [self.home] if self.home else []
        for channel in polled + sorted(self.direct):
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

    async def handle(self, post: dict[str, Any]) -> None:
        user = post["user_id"]
        direct = post["channel_id"] in self.direct
        root = post["root_id"] if direct else post["root_id"] or post["id"]
        try:
            if direct and not (
                self.home and await self.mattermost.is_member(self.home, user)
            ):
                notice = NOT_MEMBER.format(channel=self.channel_name)
                await self.mattermost.reply(post, root, notice)
                log.info("post %s: not a member", post["id"])
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
        if post["channel_id"] in self.direct:
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

    async def run(self) -> None:
        await self.start()
        log.info("polling as %s", self.me["username"])
        while True:
            try:
                await self.poll()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    raise
                log.warning("poll: HTTP %s", exc.response.status_code)
            except httpx.HTTPError as exc:
                log.warning("poll: %s", type(exc).__name__)
            HEARTBEAT.touch()
            await asyncio.sleep(POLL_SECONDS)


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
        os.environ["MATTERMOST_REFRESH_TOKEN"],
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
    )
    asyncio.run(agent.run())


if __name__ == "__main__":
    main()
