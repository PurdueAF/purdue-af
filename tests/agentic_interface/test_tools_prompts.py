"""Tests for tools/prompts.py — workflow playbooks reference real tools."""

from agentic_helpers import register_tools
from tools import prompts

EXPECTED_PROMPTS = {
    "launch_session",
    "restart_session",
    "stop_session",
    "create_cluster",
}


def test_all_prompts_registered():
    recorder = register_tools(prompts)
    assert set(recorder.prompts) == EXPECTED_PROMPTS


def test_prompts_reference_existing_tools():
    """Every tool name mentioned in a prompt must actually exist."""
    from tools import dask, logs, profiles, session, storage

    real_tools = set()
    for module in (dask, logs, profiles, session, storage):
        real_tools |= set(register_tools(module).tools)

    recorder = register_tools(prompts)
    mentioned = set()
    for fn in recorder.prompts.values():
        text = fn()
        for tool in real_tools:
            if tool in text:
                mentioned.add(tool)

    # The playbooks should steer through the session lifecycle tools.
    assert {
        "get_session_status",
        "start_af_session",
        "wait_for_session",
        "stop_af_session",
        "restart_af_session",
        "list_af_profiles",
    } <= mentioned


def test_prompt_text_never_names_unknown_tools():
    """Catch typos: any snake_case word that looks like a tool must be one."""
    import re

    from tools import dask, logs, profiles, session, storage

    real_tools = set()
    for module in (dask, logs, profiles, session, storage):
        real_tools |= set(register_tools(module).tools)

    recorder = register_tools(prompts)
    for fn in recorder.prompts.values():
        for word in re.findall(r"\b[a-z]+(?:_[a-z]+){2,}\b", fn()):
            if word.endswith(("_session", "_profiles", "_status")):
                assert word in real_tools, f"prompt references unknown tool '{word}'"
