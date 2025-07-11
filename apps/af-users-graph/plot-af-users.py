#!/usr/bin/env python3
import glob
import os
import sqlite3
from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Set up a modern, minimalistic style
plt.style.use("default")
plt.rcParams.update(
    {
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
)

# Find the latest backup file
backup_dir = "/depot/cms/purdue-af/backups"
backup_files = glob.glob(f"{backup_dir}/jupyterhub-*.sqlite")

if not backup_files:
    print("No backup files found!")
    exit(1)

# Get the most recent backup file
latest_backup = max(backup_files, key=os.path.getctime)
print(f"Using latest backup: {latest_backup}")

# Connect to the JupyterHub SQLite database
conn = sqlite3.connect(latest_backup)

# Query to get daily registered users based on the 'created' timestamp
query = """
SELECT DATE(created) AS reg_date, COUNT(*) AS num_users
FROM users
GROUP BY DATE(created)
ORDER BY reg_date;
"""
df = pd.read_sql_query(query, conn)
conn.close()

# Convert registration dates to datetime and compute cumulative registered users
df["reg_date"] = pd.to_datetime(df["reg_date"])
df["cumulative_users"] = df["num_users"].cumsum()

# Create a wide, horizontal figure
fig, ax = plt.subplots(figsize=(16, 4))

# Create a smooth gradient background
gradient = np.linspace(0, 1, 100)
ax.fill_between(
    [df["reg_date"].min(), df["reg_date"].max()],
    [0, 0],
    [df["cumulative_users"].max() * 1.1, df["cumulative_users"].max() * 1.1],
    alpha=0.02,
    color="#007ACC",
)

# Plot the line with modern styling
line = ax.plot(
    df["reg_date"],
    df["cumulative_users"],
    color="#007ACC",
    linewidth=3,
    alpha=0.9,
    solid_capstyle="round",
)

# Add subtle markers at data points
ax.scatter(
    df["reg_date"], df["cumulative_users"], color="#007ACC", s=20, alpha=0.7, zorder=5
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

# Add a subtle title
ax.text(
    0.02,
    0.98,
    "Purdue Analysis Facility Users",
    transform=ax.transAxes,
    fontsize=14,
    fontweight=600,
    color="#333333",
    verticalalignment="top",
)

# Add current user count as a prominent number
current_users = df["cumulative_users"].iloc[-1]
ax.text(
    0.98,
    0.5,
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
    0.35,
    "users",
    transform=ax.transAxes,
    fontsize=12,
    fontweight=400,
    color="#666666",
    horizontalalignment="right",
    verticalalignment="center",
)

# Tight layout
plt.tight_layout()

# Save the figure with high quality
output_path = "/data/purdue-af-registered-users.png"
plt.savefig(
    output_path, dpi=300, bbox_inches="tight", facecolor="white", edgecolor="none"
)
print(f"Plot saved as {output_path}")
