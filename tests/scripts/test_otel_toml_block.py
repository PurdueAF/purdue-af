"""Tests for docker/purdue-af/scripts/otel-toml-block.py.

This script edits a file that belongs to the user and that codex also
rewrites, in a persistent home, on every single session start. The failure
mode that matters is not "the block is missing" — it is "the config no longer
parses", which would leave the user with a broken agent and no obvious cause.
So most of what is asserted here is about not breaking the file.
"""

import tomllib

import pytest
from common import REPO, load_script

otel_block = load_script(
    REPO / "docker" / "purdue-af" / "scripts" / "otel-toml-block.py", "otel_toml_block"
)

BLOCK = """\
[otel]
environment = "purdue-af"
log_user_prompt = false

[otel.exporter.otlp-http]
endpoint = "http://alloy.cms.svc.cluster.local:4318/v1/logs"
protocol = "binary"
"""


@pytest.fixture()
def block_file(tmp_path):
    path = tmp_path / "block.toml"
    path.write_text(BLOCK)
    return path


@pytest.fixture()
def target(tmp_path):
    return tmp_path / "config.toml"


def run(block_file, target, *extra):
    return otel_block.main(["otel-toml-block.py", *extra, str(block_file), str(target)])


def test_creates_the_file_when_absent(block_file, target):
    assert run(block_file, target) == 0
    parsed = tomllib.loads(target.read_text())
    assert parsed["otel"]["environment"] == "purdue-af"
    assert parsed["otel"]["exporter"]["otlp-http"]["protocol"] == "binary"


def test_preserves_the_users_own_configuration(block_file, target):
    target.write_text(
        '# my notes\nmodel = "gpt-5"\n\n[mcp_servers.mine]\nurl = "http://x"\n'
    )
    run(block_file, target)
    parsed = tomllib.loads(target.read_text())
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["mine"]["url"] == "http://x"
    assert parsed["otel"]["environment"] == "purdue-af"
    assert "# my notes" in target.read_text()


def test_is_idempotent(block_file, target):
    target.write_text('model = "gpt-5"\n')
    run(block_file, target)
    once = target.read_text()
    run(block_file, target)
    assert target.read_text() == once


def test_replaces_an_unmarked_otel_table(block_file, target):
    """codex round-trips config.toml through its own TOML writer and does not
    promise to keep our comment markers. Appending a second [otel] table would
    make the file unparseable, so an existing one is removed either way."""
    target.write_text('[otel]\nenvironment = "stale"\nlog_user_prompt = true\n')
    run(block_file, target)
    parsed = tomllib.loads(target.read_text())
    assert parsed["otel"]["environment"] == "purdue-af"
    assert parsed["otel"]["log_user_prompt"] is False
    assert target.read_text().count("[otel]") == 1


def test_removes_nested_otel_tables_too(block_file, target):
    target.write_text(
        '[otel]\nenvironment = "stale"\n\n'
        '[otel.exporter.otlp-http]\nendpoint = "http://old"\nprotocol = "json"\n\n'
        '[mcp_servers.mine]\nurl = "http://x"\n'
    )
    run(block_file, target)
    parsed = tomllib.loads(target.read_text())
    assert parsed["otel"]["exporter"]["otlp-http"]["endpoint"].endswith("/v1/logs")
    assert parsed["mcp_servers"]["mine"]["url"] == "http://x"


def test_block_is_written_last(block_file, target):
    """A table header owns everything after it. Anywhere but the end of the
    file, a bare top-level key the user adds later would silently become part
    of [otel]."""
    target.write_text('[mcp_servers.mine]\nurl = "http://x"\n')
    run(block_file, target)
    text = target.read_text()
    assert text.index("[mcp_servers.mine]") < text.index("[otel]")


def test_a_table_named_like_otel_is_not_touched(block_file, target):
    target.write_text("[otelemetry]\nkeep = true\n")
    run(block_file, target)
    parsed = tomllib.loads(target.read_text())
    assert parsed["otelemetry"]["keep"] is True


def test_a_config_that_already_does_not_parse_is_left_alone(block_file, target):
    """Appending to a broken file would only bury the real error."""
    broken = 'model = "unterminated\n'
    target.write_text(broken)
    assert run(block_file, target) == 1
    assert target.read_text() == broken


def test_never_writes_unparseable_toml(block_file, target):
    block_file.write_text("[otel]\nthis is not toml\n")
    target.write_text('model = "gpt-5"\n')
    assert run(block_file, target) == 1
    assert target.read_text() == 'model = "gpt-5"\n'


def test_remove_takes_the_block_back_out(block_file, target):
    target.write_text('model = "gpt-5"\n')
    run(block_file, target)
    assert otel_block.main(["otel-toml-block.py", "--remove", str(target)]) == 0
    parsed = tomllib.loads(target.read_text())
    assert "otel" not in parsed
    assert parsed["model"] == "gpt-5"


def test_remove_deletes_a_file_that_held_only_the_block(block_file, target):
    run(block_file, target)
    otel_block.main(["otel-toml-block.py", "--remove", str(target)])
    assert not target.exists()


def test_remove_on_a_missing_file_is_a_no_op(target):
    assert otel_block.main(["otel-toml-block.py", "--remove", str(target)]) == 0
