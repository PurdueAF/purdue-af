"""Every page under docs/docs is in the site navigation in zensical.toml, and
every nav entry is a page that exists."""

import tomllib

from common import REPO

DOCS = REPO / "docs" / "docs"
CONFIG = REPO / "docs" / "zensical.toml"


def pages(nav) -> set[str]:
    found = set()
    for entry in nav:
        values = entry.values() if isinstance(entry, dict) else [entry]
        for value in values:
            if isinstance(value, list):
                found |= pages(value)
            elif isinstance(value, str) and value.endswith(".md"):
                found.add(value)
    return found


def test_nav_and_pages_are_the_same_set():
    with CONFIG.open("rb") as f:
        nav = tomllib.load(f)["project"]["nav"]
    on_disk = {str(p.relative_to(DOCS)) for p in DOCS.rglob("*.md")}
    assert pages(nav) == on_disk
