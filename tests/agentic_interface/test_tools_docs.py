"""Tests for tools/docs.py — the user documentation, by page."""

import pytest
from agentic_helpers import failure, register_tools
from common import EXPERIMENTAL, REPO, generator, load_yaml
from errors import UpstreamError
from tools import docs


@pytest.fixture
def pages(tmp_path, monkeypatch):
    (tmp_path / "index.md").write_text("# Purdue Analysis Facility\n\nWelcome.\n")
    (tmp_path / "storage.md").write_text("# Storage volumes\n\n## Quotas\n\n25 GB.\n")
    monkeypatch.setattr(docs, "DOCS_DIR", str(tmp_path))
    return tmp_path


def read(page):
    return register_tools(docs).tools["read_af_doc"](page)


@pytest.mark.parametrize("asked", ["storage", "storage.md", "storage.md#quotas"])
async def test_a_page_comes_with_its_published_address(pages, asked):
    out = await read(asked)
    assert out.startswith(f"{docs.DOCS_URL}/storage/\n\n# Storage volumes")
    assert "25 GB." in out


async def test_the_front_page_is_published_at_the_root(pages):
    assert (await read("index")).startswith(f"{docs.DOCS_URL}/\n\n")


async def test_an_unknown_page_lists_the_pages(pages):
    out = await failure(read("quotas"))
    assert "no page 'quotas'" in out
    assert "- storage: Storage volumes" in out


async def test_pages_are_read_as_they_are_now(pages):
    (pages / "gpus.md").write_text("# GPU access\n")
    assert "- gpus: GPU access" in docs.index()
    assert "# GPU access" in await read("gpus")


async def test_no_mounted_pages_is_the_facilitys_fault(pages):
    for page in pages.glob("*.md"):
        page.unlink()
    with pytest.raises(UpstreamError, match=docs.DOCS_URL):
        await read("storage")


def test_the_deployment_mounts_pages_its_flux_root_generates():
    spec = load_yaml(REPO / "apps/agentic-interface/deployment.yaml")["spec"]
    pod = spec["template"]["spec"]
    (volume,) = [v for v in pod["volumes"] if v["name"] == "docs"]
    (mount,) = [m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "docs"]
    assert generator(load_yaml(EXPERIMENTAL), volume["configMap"]["name"])["files"]
    assert mount["mountPath"] == "/docs"
