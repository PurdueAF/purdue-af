"""The hub backup script, against a stub kubectl that runs the real copy step."""

import os
import sqlite3
import subprocess
import sys

import pytest
from common import REPO

SCRIPT = REPO / "apps/jupyterhub/database-backup/backup-database.sh"
RUNTIME_LINE = "nvidia driver modules are not yet loaded, invoking runc directly\n"

STUB = """\
#!{python}
import os, subprocess, sys

args = sys.argv[1:]
if args[0] == "get":
    print("hub-0")
elif "python3" in args:
    code = args[args.index("-c") + 1]
    done = subprocess.run([sys.executable, "-c", code, args[-1]], capture_output=True)
    sys.stderr.buffer.write(done.stderr)
    body = done.stdout
    sys.stdout.write(os.environ.get("RUNTIME_LINE", ""))
    sys.stdout.flush()
    keep = os.environ.get("KEEP_BYTES")
    sys.stdout.buffer.write(body[: int(keep)] if keep else body)
    sys.exit(done.returncode)
"""


@pytest.fixture
def backup(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "kubectl"
    stub.write_text(STUB.format(python=sys.executable))
    stub.chmod(0o755)

    db = tmp_path / "jupyterhub.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("create table users (name text)")
        conn.executemany("insert into users values (?)", [("a",), ("b",)])

    backups = tmp_path / "backups"
    script = tmp_path / "backup-database.sh"
    text = SCRIPT.read_text()
    for literal, value in (
        ("/depot/cms/purdue-af/backups", str(backups)),
        ("/srv/jupyterhub/jupyterhub.sqlite", str(db)),
    ):
        assert literal in text
        text = text.replace(literal, value)
    script.write_text(text)

    def run(**env):
        path = f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
        return subprocess.run(
            ["sh", str(script)],
            env={**os.environ, "PATH": path, **env},
            capture_output=True,
            text=True,
        )

    run.backups = backups
    run.db = db
    return run


def rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("select name from users order by name").fetchall()


def test_a_line_the_node_runtime_prints_stays_out_of_the_backup(backup):
    """kubectl exec's stdout carries it, and a Job that wrote it into the file
    still succeeded while no copy could be opened."""
    result = backup(RUNTIME_LINE=RUNTIME_LINE)
    assert result.returncode == 0, result.stdout + result.stderr
    (copy,) = backup.backups.iterdir()
    assert copy.read_bytes().startswith(b"SQLite format 3\0")
    assert rows(copy) == [("a",), ("b",)]


def test_a_cut_transfer_fails_the_job_and_keeps_the_last_good_copy(backup):
    assert backup().returncode == 0
    (good,) = backup.backups.iterdir()
    before = good.read_bytes()
    good.unlink()
    good.write_bytes(before)
    os.utime(good, (1, 1))

    result = backup(KEEP_BYTES="400")
    assert result.returncode != 0
    assert "checksum" in result.stdout
    assert [p.name for p in backup.backups.iterdir()] == [good.name]
    assert good.read_bytes() == before


def test_a_damaged_database_fails_the_job(backup):
    data = bytearray(backup.db.read_bytes())
    data[4096:8192] = b"\xff" * 4096
    backup.db.write_bytes(data)
    assert backup().returncode != 0
    assert list(backup.backups.iterdir()) == []


def test_only_the_fourteen_newest_copies_stay(backup):
    backup.backups.mkdir()
    for n in range(20):
        old = backup.backups / f"jupyterhub-old{n:02}.sqlite"
        old.write_text("old")
        os.utime(old, (n + 1, n + 1))
    assert backup().returncode == 0
    assert len(list(backup.backups.iterdir())) == 14
    assert not (backup.backups / "jupyterhub-old00.sqlite").exists()
    assert (backup.backups / "jupyterhub-old19.sqlite").exists()
