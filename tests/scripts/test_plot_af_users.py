"""Tests for apps/af-utils/af-users-graph/plot-af-users.py."""

import datetime
import sqlite3

import pytest


def make_hub_db(path, created_timestamps):
    """Create a minimal JupyterHub-like users table with given 'created' stamps."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, created TIMESTAMP)"
    )
    for i, ts in enumerate(created_timestamps):
        conn.execute(
            "INSERT INTO users (name, created) VALUES (?, ?)", (f"user{i}", ts)
        )
    conn.commit()
    conn.close()
    return path


def test_main_plots_the_daily_running_total(plot_af_users, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    db = make_hub_db(
        backup_dir / "jupyterhub-2024-06-01.sqlite",
        [
            "2024-03-05 12:00:00.123456",
            "2024-01-01 10:00:00",
            "2024-01-01 15:30:00",
        ],
    )
    out = tmp_path / "users.png"

    plot_af_users.main(backup_dir=str(backup_dir), output_path=str(out))

    assert out.read_bytes().startswith(b"\x89PNG")
    stats = plot_af_users.load_registration_stats(db)
    assert stats.reg_dates == [datetime.date(2024, 1, 1), datetime.date(2024, 3, 5)]
    assert stats.cumulative_users == [2, 3]


def test_the_newest_backup_by_ctime_wins(plot_af_users, tmp_path, monkeypatch):
    old = tmp_path / "jupyterhub-2024-06-01.sqlite"
    new = tmp_path / "jupyterhub-2024-01-01.sqlite"
    decoy = tmp_path / "other.sqlite"
    for path in (old, new, decoy):
        path.touch()
    ctimes = {str(old): 100.0, str(new): 200.0, str(decoy): 300.0}
    monkeypatch.setattr(plot_af_users.os.path, "getctime", lambda p: ctimes[str(p)])

    assert plot_af_users.find_latest_backup(str(tmp_path)) == str(new)


def test_main_exits_when_there_are_no_backups(plot_af_users, tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        plot_af_users.main(backup_dir=str(tmp_path))

    assert excinfo.value.code == 1
