"""The purdue-af plugin: .claude-plugin/README.md says which file is what."""

import json

from common import REPO

SERVER = "purdue-af-agentic-interface"
PLUGIN_ROOT = REPO / ".claude"


def _load(path):
    return json.loads(path.read_text())


def test_marketplace_lists_the_plugin_at_its_root():
    (plugin,) = _load(REPO / ".claude-plugin" / "marketplace.json")["plugins"]
    manifest = _load(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    assert (REPO / plugin["source"]).resolve() == PLUGIN_ROOT
    for field in ("name", "description"):
        assert plugin[field] == manifest[field]
    assert (PLUGIN_ROOT / "skills" / SERVER / "SKILL.md").is_file()
    assert "version" not in plugin and "version" not in manifest


def test_plugin_server_is_the_one_the_repository_uses():
    project = _load(REPO / ".mcp.json")["mcpServers"][SERVER]
    plugin = _load(PLUGIN_ROOT / ".mcp.json")["mcpServers"][SERVER]
    assert plugin["url"] == project["url"]
    assert plugin["headersHelper"] == project["headersHelper"]
