#!/usr/bin/env python3
"""Plot cumulative registered AF users from the latest JupyterHub DB backup."""

import glob
import os
import sqlite3
import sys

import matplotlib.pyplot as plt
import pandas as pd

BACKUP_DIR = "/depot/cms/purdue-af/backups"
OUTPUT_PATH = "/data/purdue-af-registered-users.png"

# Modern, minimalistic style
PLOT_STYLE = {
    "font.family": "sans-serif",
    "font.size": 12,
    "axes.linewidth": 0.8,
    "axes.edgecolor": "#E0E0E0",
    "axes.facecolor": "white",
    "figure.facecolor": "white",
    "grid.color": "#F0F0F0",
    "grid.linestyle": "-",
    "grid.linewidth": 0.5,
    "grid.alpha": 0.8,
    "xtick.color": "#666666",
    "ytick.color": "#666666",
    "axes.labelcolor": "#333333",
    "axes.titlecolor": "#333333",
}

# Query to get daily registered users based on the 'created' timestamp
REGISTRATIONS_QUERY = """
SELECT DATE(created) AS reg_date, COUNT(*) AS num_users
FROM users
GROUP BY DATE(created)
ORDER BY reg_date;
"""


def find_latest_backup(backup_dir):
    """Return the most recent jupyterhub-*.sqlite backup, or None if there are none."""
    backup_files = glob.glob(f"{backup_dir}/jupyterhub-*.sqlite")
    if not backup_files:
        return None
    return max(backup_files, key=os.path.getctime)


def load_registration_stats(db_path):
    """Daily and cumulative registered-user counts from a JupyterHub database."""
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(REGISTRATIONS_QUERY, conn)
    finally:
        conn.close()

    # Convert registration dates to datetime and compute cumulative registered users
    df["reg_date"] = pd.to_datetime(df["reg_date"])
    df["cumulative_users"] = df["num_users"].cumsum()
    return df


def plot_registered_users(df, output_path):
    """Render the cumulative-users plot and save it to output_path."""
    plt.style.use("default")
    plt.rcParams.update(PLOT_STYLE)

    # Create a wide, horizontal figure
    fig, ax = plt.subplots(figsize=(16, 4))

    # A smooth gradient-like background wash
    ax.fill_between(
        [df["reg_date"].min(), df["reg_date"].max()],
        [0, 0],
        [df["cumulative_users"].max() * 1.1, df["cumulative_users"].max() * 1.1],
        alpha=0.02,
        color="#007ACC",
    )

    # Plot the line with modern styling
    ax.plot(
        df["reg_date"],
        df["cumulative_users"],
        color="#007ACC",
        linewidth=3,
        alpha=0.9,
        solid_capstyle="round",
    )

    # Add a subtle area fill under the line
    ax.fill_between(df["reg_date"], df["cumulative_users"], alpha=0.1, color="#007ACC")

    # Customize the grid
    ax.grid(True, alpha=0.3, linestyle="-", linewidth=0.5)

    # Remove spines except bottom
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.spines["bottom"].set_visible(True)
    ax.spines["bottom"].set_color("#E0E0E0")
    ax.spines["bottom"].set_linewidth(0.8)

    # Customize ticks
    ax.tick_params(axis="both", colors="#666666", labelsize=10)
    ax.tick_params(axis="x", rotation=0)

    # Set axis limits with some padding
    ax.set_ylim(0, df["cumulative_users"].max() * 1.05)
    ax.set_xlim(
        df["reg_date"].min() - pd.Timedelta(days=5),
        df["reg_date"].max() + pd.Timedelta(days=5),
    )

    # Remove axis labels for minimalistic look
    ax.set_xlabel("")
    ax.set_ylabel("")

    # Add current user count as a prominent number
    current_users = df["cumulative_users"].iloc[-1]
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

    # Add "users" label
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

    # Tight layout
    plt.tight_layout()

    # Save the figure with high quality
    plt.savefig(
        output_path, dpi=300, bbox_inches="tight", facecolor="white", edgecolor="none"
    )
    plt.close(fig)


def main(backup_dir=BACKUP_DIR, output_path=OUTPUT_PATH):
    latest_backup = find_latest_backup(backup_dir)
    if latest_backup is None:
        print("No backup files found!")
        sys.exit(1)
    print(f"Using latest backup: {latest_backup}")

    df = load_registration_stats(latest_backup)
    plot_registered_users(df, output_path)
    print(f"Plot saved as {output_path}")


if __name__ == "__main__":
    main()
