"""Tripwire: the two copies of the agent skill must never drift.

The canonical, user-downloadable skill lives next to the server source
(docker/agentic-interface/purdue-af-agentic-interface.skill.md, linked from the
user docs); .claude/skills/ holds the project-scoped Claude Code copy.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CANONICAL = (
    REPO / "docker" / "agentic-interface" / "purdue-af-agentic-interface.skill.md"
)
PROJECT_COPY = REPO / ".claude" / "skills" / "purdue-af-agentic-interface" / "SKILL.md"


def test_skill_copies_are_identical():
    assert CANONICAL.read_text() == PROJECT_COPY.read_text(), (
        "Skill files have drifted. Edit the canonical file "
        "(docker/agentic-interface/purdue-af-agentic-interface.skill.md) and copy "
        "it to .claude/skills/purdue-af-agentic-interface/SKILL.md"
    )


def test_skill_has_frontmatter_required_by_claude_code():
    text = CANONICAL.read_text()
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    assert "name: purdue-af-agentic-interface" in frontmatter
    assert "description:" in frontmatter
