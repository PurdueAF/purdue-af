"""apps/mattermost-agent/evaluate.py against a fake LLM proxy and MCP server."""

import sys

import httpx
import pytest
from common import REPO, load_script
from test_agent import FakeFacility, FakeLLM, tool_call

evaluate = load_script(REPO / "apps" / "mattermost-agent" / "evaluate.py", "evaluate")


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Runs evaluate.py over the given cases; the LLM answers with the replies."""
    facilities = []

    def facility(url, token_file):
        facilities.append(FakeFacility())
        return facilities[-1]

    for name, token in (("bot", "bot-token"), ("person", "person-token"), ("llm", "x")):
        (tmp_path / name).write_text(token)

    async def go(cases, *replies, repeat=1):
        llm = FakeLLM(*replies)
        llm.personal = cases[0].get("intent", "general")
        client = httpx.AsyncClient
        monkeypatch.setattr(evaluate.agent, "Facility", facility)
        monkeypatch.setattr(
            evaluate.httpx,
            "AsyncClient",
            lambda **kwargs: client(
                base_url="http://llm", transport=httpx.MockTransport(llm)
            ),
        )
        (tmp_path / "cases.yaml").write_text(evaluate.yaml.safe_dump(cases))
        argv = ["evaluate.py", "--cases", tmp_path / "cases.yaml"]
        argv += ["--token-file", tmp_path / "bot"]
        argv += ["--person-token-file", tmp_path / "person"]
        argv += ["--llm-token-file", tmp_path / "llm", "--repeat", repeat]
        argv += ["--out", tmp_path / "out.json"]
        monkeypatch.setattr(sys, "argv", [str(a) for a in argv])
        status = await evaluate.main()
        results = evaluate.json.loads((tmp_path / "out.json").read_text())
        return status, results, facilities

    return go


def test_an_answer_is_judged_by_its_tools_its_text_and_its_intent():
    case = {
        "intent": "general",
        "tools_any": ["get_ray_capacity"],
        "answer": [r"\d"],
        "answer_not": ["cannot"],
    }
    good = {"intent": "general", "tools": ["get_ray_capacity"], "answer": "4 free."}
    assert evaluate.judge(case, good) == []
    bad = {"intent": "personal", "tools": ["query_prometheus"], "answer": "I cannot."}
    assert evaluate.judge(case, bad) == [
        "intent personal, not general",
        "called none of ['get_ray_capacity']",
        r"answer lacks /\d/",
        "answer has /cannot/",
    ]


async def test_a_general_question_is_asked_as_the_bot(run):
    case = {"name": "health", "ask": "is it up?", "tools_any": ["get_facility_health"]}
    status, results, facilities = await run(
        [case], tool_call("get_facility_health"), {"content": "It is up."}
    )
    assert status == 0
    assert results[0]["wrong"] == []
    assert results[0]["runs"][0]["tools"] == ["get_facility_health"]
    assert results[0]["runs"][0]["answer"] == "It is up."
    assert facilities[0].tokens == [None]


async def test_a_linked_question_is_asked_with_the_persons_token(run):
    case = {"name": "mine", "ask": "is my session running?", "linked": True}
    case["intent"] = "personal"
    status, results, facilities = await run([case], {"content": "It is running."})
    assert status == 0
    assert results[0]["runs"][0]["intent"] == "personal"
    assert facilities[0].tokens == ["person-token"]


async def test_one_failing_run_fails_a_repeated_case(run):
    case = {"name": "count", "ask": "how many?", "answer": [r"\d"]}
    status, results, _ = await run(
        [case], {"content": "3 of them."}, {"content": "Some."}, repeat=2
    )
    assert status == 1
    assert results[0]["wrong"] == [r"run 2: answer lacks /\d/"]
