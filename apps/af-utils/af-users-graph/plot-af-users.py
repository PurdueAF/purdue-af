#!/usr/bin/env python3
"""Plot cumulative registered AF users from the latest JupyterHub DB backup."""

import glob
import itertools
import os
import sqlite3
import sys
from datetime import date, timedelta
from typing import NamedTuple

import matplotlib.pyplot as plt

BACKUP_DIR = "/backups"
OUTPUT_PATH = "/data/purdue-af-registered-users.png"

PLOT_STYLE = {
    "font.size": 12,
    "axes.edgecolor": "#E0E0E0",
    "grid.color": "#F0F0F0",
    "grid.linewidth": 0.5,
    "grid.alpha": 0.8,
    "xtick.color": "#666666",
    "ytick.color": "#666666",
    "axes.labelcolor": "#333333",
    "axes.titlecolor": "#333333",
}

REGISTRATIONS_QUERY = """
SELECT DATE(created) AS reg_date, COUNT(*) AS num_users
FROM users
GROUP BY DATE(created)
ORDER BY reg_date;
"""


class RegistrationStats(NamedTuple):
    reg_dates: list[date]
    cumulative_users: list[int]


def find_latest_backup(backup_dir: str) -> str | None:
    """Return the most recent jupyterhub-*.sqlite backup, or None if there are none."""
    backup_files = glob.glob(f"{backup_dir}/jupyterhub-*.sqlite")
    if not backup_files:
        return None
    return max(backup_files, key=os.path.getctime)


def load_registration_stats(db_path: str) -> RegistrationStats:
    """Registration dates and the running user total at each, oldest first."""
    conn = sqlite3.connect(db_path)
    try:
        rows: list[tuple[str, int]] = conn.execute(REGISTRATIONS_QUERY).fetchall()
    finally:
        conn.close()

    return RegistrationStats(
        reg_dates=[date.fromisoformat(reg_date) for reg_date, _ in rows],
        cumulative_users=list(itertools.accumulate(count for _, count in rows)),
    )


def plot_registered_users(stats: RegistrationStats, output_path: str) -> None:
    """Render the cumulative-users plot and save it to output_path."""
    reg_dates, cumulative_users = stats
    first_date, last_date = reg_dates[0], reg_dates[-1]
    current_users = cumulative_users[-1]

    plt.rcParams.update(PLOT_STYLE)

    fig, ax = plt.subplots(figsize=(16, 4))

    ax.fill_between(
        [first_date, last_date],
        [0, 0],
        [current_users * 1.1, current_users * 1.1],
        alpha=0.02,
        color="#007ACC",
    )

    ax.plot(
        reg_dates,
        cumulative_users,
        color="#007ACC",
        linewidth=3,
        alpha=0.9,
        solid_capstyle="round",
    )

    ax.fill_between(reg_dates, cumulative_users, alpha=0.1, color="#007ACC")

    ax.grid(True, alpha=0.3, linestyle="-", linewidth=0.5)

    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.spines["bottom"].set_visible(True)
    ax.spines["bottom"].set_color("#E0E0E0")
    ax.spines["bottom"].set_linewidth(0.8)

    ax.tick_params(axis="both", colors="#666666", labelsize=10)

    ax.set_ylim(0, current_users * 1.05)
    ax.set_xlim(first_date - timedelta(days=5), last_date + timedelta(days=5))

    ax.text(
        0.98,
        0.45,
        f"{current_users}",
        transform=ax.transAxes,
        fontsize=48,
        fontweight=1000,
        color="#007ACC",
        horizontalalignment="right",
        verticalalignment="center",
    )

    ax.text(
        0.98,
        0.33,
        "registered users",
        transform=ax.transAxes,
        fontsize=22,
        fontweight=400,
        color="#666666",
        horizontalalignment="right",
        verticalalignment="center",
    )

    plt.tight_layout()

    plt.savefig(
        output_path, dpi=300, bbox_inches="tight", facecolor="white", edgecolor="none"
    )
    plt.close(fig)


def main(backup_dir: str = BACKUP_DIR, output_path: str = OUTPUT_PATH) -> None:
    latest_backup = find_latest_backup(backup_dir)
    if latest_backup is None:
        print("No backup files found!")
        sys.exit(1)
    print(f"Using latest backup: {latest_backup}")

    plot_registered_users(load_registration_stats(latest_backup), output_path)
    print(f"Plot saved as {output_path}")


if __name__ == "__main__":
    main()
