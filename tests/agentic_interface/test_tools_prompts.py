"""Tests for tools/prompts.py — workflow playbooks reference real tools."""

from agentic_helpers import register_tools
from tools import prompts


def test_prompt_text_never_names_unknown_tools():
    """Catch typos: any word with two or more underscores must be a tool."""
    import re

    from tools import dask, logs, profiles, session, storage

    real_tools = set()
    for module in (dask, logs, profiles, session, storage):
        real_tools |= set(register_tools(module).tools)

    recorder = register_tools(prompts)
    for fn in recorder.prompts.values():
        words = re.findall(r"\b[a-z]+(?:_[a-z]+){2,}\b", fn())
        assert words
        for word in words:
            assert word in real_tools, f"prompt references unknown tool '{word}'"
