"""Sanity checks for the agent skill file.

The single canonical skill lives at .claude/skills/purdue-af-agentic-interface/
SKILL.md — project-scoped for Claude Code users of this repo, and linked from
the user docs for everyone else.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / ".claude" / "skills" / "purdue-af-agentic-interface" / "SKILL.md"


def test_skill_exists():
    assert SKILL.is_file(), (
        "Skill file missing — the user docs link to "
        ".claude/skills/purdue-af-agentic-interface/SKILL.md"
    )


def test_skill_has_frontmatter_required_by_claude_code():
    text = SKILL.read_text()
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    assert "name: purdue-af-agentic-interface" in frontmatter
    assert "description:" in frontmatter
