"""Asks the bot's own answer path the questions in evaluate.yaml, against the live
agentic interface and LLM proxy, and reports which answers pass. Mattermost and the
Hub are stood in for; the conversation is the question alone.

Run from a checkout, with the LLM proxy forwarded and its client token in a file:

    kubectl -n cms port-forward svc/agentic-analysis-llm-proxy 8080
    uv run --project tests --frozen python apps/mattermost-agent/evaluate.py \\
        --llm-token-file <file>

It calls the MCP server with the token in --token-file, a person's, so public
answers may also see that person's own series; who sees what is the unit tests'.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
spec = importlib.util.spec_from_file_location("agent", HERE / "agent.py")
assert spec and spec.loader
agent = importlib.util.module_from_spec(spec)
sys.modules["agent"] = agent
spec.loader.exec_module(agent)

ME = {"id": "bot", "username": "purdueaf-agent"}
ASKER = "asker"


class Thread:
    """Mattermost as the bot sees one question: the post is the conversation."""

    def __init__(self, post: dict[str, Any]) -> None:
        self.post = post

    async def get(self, path: str, **params: Any) -> Any:
        return ME

    async def thread(self, post_id: str) -> list[dict[str, Any]]:
        return [self.post]

    async def recent(self, channel: str, count: int) -> list[dict[str, Any]]:
        return [self.post]


class Hub:
    def __init__(self, name: str) -> None:
        self.name = name

    async def owner(self, token: str) -> str:
        return self.name


class Linked:
    """A person linked or not, with the token of --token-file."""

    def __init__(self, token: str | None) -> None:
        self.hub = Hub("asker")
        self.linked_token = token

    def token(self, user: str) -> str | None:
        return self.linked_token

    def until(self, user: str) -> float | None:
        return time.time() + 86400 if self.linked_token else None

    def forget(self, user: str) -> None:
        pass

    def link_url(self, user: str) -> str:
        return (
            "https://cms.geddes.rcac.purdue.edu/services/mattermost-agent/link?state=x"
        )


async def ask(case: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    token = args.token_file.read_text().strip()
    private = bool(case.get("private") or case.get("linked"))
    post: dict[str, Any] = {
        "id": "question",
        "channel_id": "dm" if private else "channel",
        "root_id": "",
        "user_id": ASKER,
        "create_at": int(time.time() * 1000),
        "update_at": int(time.time() * 1000),
        "delete_at": 0,
        "type": "",
        "message": case["ask"] if private else f"@{ME['username']} {case['ask']}",
        "props": {},
    }
    llm = httpx.AsyncClient(
        base_url=args.llm_url,
        headers={"Authorization": f"Bearer {args.llm_token_file.read_text().strip()}"},
        timeout=agent.ANSWER_SECONDS,
    )
    bot = agent.Agent(
        Thread(post),
        llm,
        agent.Facility(args.mcp_url, args.token_file),
        agent.Docs(REPO / "docs" / "docs"),
        HERE / "system-prompt.md",
        args.role,
        "cms-exp",
        "purdue-analysis-facility",
        None,
        Linked(token if case.get("linked") else None),
    )
    bot.me = ME
    bot.mention = re.compile(rf"(?<![\w.-])@{ME['username']}(?![\w-])", re.IGNORECASE)
    bot.channel_name = "Purdue Analysis Facility"
    if private:
        bot.direct = {"dm"}
    seen: dict[str, Any] = {"tools": [], "intent": None}
    call, intent = bot.call, bot.intent

    async def calling(*a: Any, **k: Any) -> str:
        seen["tools"].append(a[1])
        result: str = await call(*a, **k)
        return result

    async def judging(*a: Any, **k: Any) -> str:
        verdict: str = await intent(*a, **k)
        seen["intent"] = verdict
        return verdict

    setattr(bot, "call", calling)
    setattr(bot, "intent", judging)
    started = time.monotonic()
    answer = await bot.answer_or_why(post)
    seen.update(answer=answer, seconds=round(time.monotonic() - started))
    return seen


def judge(case: dict[str, Any], seen: dict[str, Any]) -> list[str]:
    """What is wrong with an answer, by the case's expectations."""
    wrong = []
    if case.get("intent") and seen["intent"] != case["intent"]:
        wrong.append(f"intent {seen['intent']}, not {case['intent']}")
    wanted = case.get("tools_any") or []
    if wanted and not set(wanted) & set(seen["tools"]):
        wrong.append(f"called none of {wanted}")
    for pattern in case.get("answer") or []:
        if not re.search(pattern, seen["answer"], re.IGNORECASE | re.DOTALL):
            wrong.append(f"answer lacks /{pattern}/")
    for pattern in case.get("answer_not") or []:
        if re.search(pattern, seen["answer"], re.IGNORECASE | re.DOTALL):
            wrong.append(f"answer has /{pattern}/")
    if agent.LEAK.search(seen["answer"]):
        wrong.append("answer holds markup or reasoning")
    return wrong


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cases", type=Path, default=HERE / "evaluate.yaml")
    parser.add_argument("--only", help="Run the cases whose name contains this.")
    parser.add_argument(
        "--mcp-url",
        default="https://cms.geddes.rcac.purdue.edu/services/agentic-interface/mcp",
    )
    parser.add_argument(
        "--token-file", type=Path, default=Path.home() / ".config/purdue-af/token"
    )
    parser.add_argument("--llm-url", default="http://localhost:8080")
    parser.add_argument("--llm-token-file", type=Path, required=True)
    parser.add_argument("--role", default="facility-assistant")
    parser.add_argument("--out", type=Path, help="Write each answer here, as JSON.")
    args = parser.parse_args()
    cases = yaml.safe_load(args.cases.read_text())
    if args.only:
        cases = [c for c in cases if args.only in c["name"]]
    results = []
    for case in cases:
        try:
            seen = await ask(case, args)
            wrong = judge(case, seen)
        except Exception as exc:  # a broken case is reported, not fatal
            seen, wrong = {"tools": [], "answer": ""}, [f"{type(exc).__name__}: {exc}"]
        mark = "pass" if not wrong else "FAIL"
        tools = ",".join(seen["tools"]) or "-"
        print(f"{mark}  {case['name']:<34} tools={tools}", flush=True)
        for line in wrong:
            print(f"      {line}", flush=True)
        results.append({"case": case, "seen": seen, "wrong": wrong})
    passed = sum(not r["wrong"] for r in results)
    print(f"\n{passed} of {len(results)} passed")
    if args.out:
        args.out.write_text(json.dumps(results, indent=2, default=str))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
