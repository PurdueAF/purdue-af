"""Tests for the data-handling logic of apps/af-utils/af-users-graph/plot-af-users.py."""

import sqlite3

import pandas as pd
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


class TestFindLatestBackup:
    def test_no_backups_returns_none(self, plot_af_users, tmp_path):
        assert plot_af_users.find_latest_backup(str(tmp_path)) is None

    def test_missing_dir_returns_none(self, plot_af_users, tmp_path):
        assert plot_af_users.find_latest_backup(str(tmp_path / "nope")) is None

    def test_picks_newest_by_ctime(self, plot_af_users, tmp_path, monkeypatch):
        old = tmp_path / "jupyterhub-2024-01-01.sqlite"
        new = tmp_path / "jupyterhub-2024-06-01.sqlite"
        old.touch()
        new.touch()
        ctimes = {str(old): 100.0, str(new): 200.0}
        monkeypatch.setattr(plot_af_users.os.path, "getctime", lambda p: ctimes[str(p)])
        assert plot_af_users.find_latest_backup(str(tmp_path)) == str(new)

    def test_ignores_non_backup_files(self, plot_af_users, tmp_path):
        (tmp_path / "other.sqlite").touch()
        (tmp_path / "jupyterhub-2024.txt").touch()
        assert plot_af_users.find_latest_backup(str(tmp_path)) is None


class TestLoadRegistrationStats:
    def test_daily_grouping_and_cumsum(self, plot_af_users, tmp_path):
        db = make_hub_db(
            tmp_path / "hub.sqlite",
            [
                "2024-01-01 10:00:00",
                "2024-01-01 15:30:00",
                "2024-01-03 09:00:00",
            ],
        )
        df = plot_af_users.load_registration_stats(db)

        assert list(df["reg_date"]) == [
            pd.Timestamp("2024-01-01"),
            pd.Timestamp("2024-01-03"),
        ]
        assert list(df["num_users"]) == [2, 1]
        assert list(df["cumulative_users"]) == [2, 3]

    def test_dates_sorted_and_datetime_typed(self, plot_af_users, tmp_path):
        db = make_hub_db(
            tmp_path / "hub.sqlite",
            ["2024-03-05 12:00:00", "2024-01-20 12:00:00", "2024-02-10 12:00:00"],
        )
        df = plot_af_users.load_registration_stats(db)

        assert pd.api.types.is_datetime64_any_dtype(df["reg_date"])
        assert df["reg_date"].is_monotonic_increasing
        assert df["cumulative_users"].iloc[-1] == 3

    def test_single_user(self, plot_af_users, tmp_path):
        db = make_hub_db(tmp_path / "hub.sqlite", ["2024-01-01 10:00:00"])
        df = plot_af_users.load_registration_stats(db)
        assert len(df) == 1
        assert df["cumulative_users"].iloc[0] == 1


class TestPlot:
    def test_writes_png(self, plot_af_users, tmp_path):
        df = pd.DataFrame(
            {
                "reg_date": pd.to_datetime(["2024-01-01", "2024-01-10", "2024-02-01"]),
                "num_users": [2, 1, 4],
            }
        )
        df["cumulative_users"] = df["num_users"].cumsum()
        out = tmp_path / "users.png"
        plot_af_users.plot_registered_users(df, str(out))
        assert out.stat().st_size > 0


class TestMain:
    def test_end_to_end(self, plot_af_users, tmp_path, capsys):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        make_hub_db(
            backup_dir / "jupyterhub-2024-06-01.sqlite",
            ["2024-01-01 10:00:00", "2024-01-02 10:00:00"],
        )
        out = tmp_path / "users.png"
        plot_af_users.main(backup_dir=str(backup_dir), output_path=str(out))

        captured = capsys.readouterr()
        assert "Using latest backup:" in captured.out
        assert f"Plot saved as {out}" in captured.out
        assert out.stat().st_size > 0

    def test_exits_when_no_backups(self, plot_af_users, tmp_path, capsys):
        with pytest.raises(SystemExit) as excinfo:
            plot_af_users.main(backup_dir=str(tmp_path))
        assert excinfo.value.code == 1
        assert "No backup files found!" in capsys.readouterr().out
