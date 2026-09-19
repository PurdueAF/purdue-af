"""The pre-commit hook that keeps every relative Markdown link resolving."""

from common import REPO, load_script

check = load_script(
    REPO / ".github/workflows/check-markdown-links.py", "check_markdown_links"
)


def write(tmp_path, name, text):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_a_resolving_link_passes(tmp_path):
    write(tmp_path, "other.md", "# other")
    (tmp_path / "dir").mkdir()
    doc = write(
        tmp_path,
        "doc.md",
        "[a](other.md) [b](other.md#section) [c](dir) [d](dir/) ![e](<other.md>)\n",
    )
    assert check.broken(doc) == []


def test_a_missing_target_is_reported_with_its_line(tmp_path):
    doc = write(tmp_path, "doc.md", "fine\n\n[gone](nope.md)\n[ref]: also/gone.md\n")
    assert check.broken(doc) == [
        f"{doc}:3: broken link -> nope.md",
        f"{doc}:4: broken link -> also/gone.md",
    ]


def test_external_anchor_and_absolute_links_are_ignored(tmp_path):
    doc = write(
        tmp_path,
        "doc.md",
        "[h](https://x.invalid/y.md) [m](mailto:a@b) [a](#top) [r](/root.md)\n",
    )
    assert check.broken(doc) == []


def test_links_inside_fenced_code_are_ignored_and_lines_kept(tmp_path):
    doc = write(
        tmp_path,
        "doc.md",
        "```\n[x](in-code.md)\n```\n[y](real-miss.md)\n",
    )
    assert check.broken(doc) == [f"{doc}:4: broken link -> real-miss.md"]


def test_main_exits_nonzero_on_a_broken_link(tmp_path, capsys):
    good = write(tmp_path, "good.md", "[a](good.md)\n")
    bad = write(tmp_path, "bad.md", "[a](missing.md)\n")
    assert check.main([str(good)]) == 0
    assert check.main([str(good), str(bad)]) == 1
    assert "missing.md" in capsys.readouterr().out


def test_every_tracked_markdown_file_passes():
    """The hook's real job: the repository's own links."""
    failures = [
        f
        for path in REPO.rglob("*.md")
        if ".pixi" not in path.parts and ".venv" not in path.parts
        for f in check.broken(path)
    ]
    assert failures == []
