#!/usr/bin/env python3
import sqlite3
import os
import glob
from datetime import datetime

import matplotlib.pyplot as plt
import mplhep as hep
import pandas as pd

# Apply the CMS style from mplhep
hep.style.use(hep.style.CMS)

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

# Create the cumulative plot using mplhep's CMS style
plt.figure(figsize=(12, 9))
plt.plot(df["reg_date"], df["cumulative_users"], marker="o", linestyle="-")

# Set labels and title as specified
plt.xlabel("Date")
plt.ylabel("Registered Users")
plt.title("Purdue Analysis Facility - Registered Users")
plt.xticks(rotation=35)
plt.ylim(0, 250)
plt.grid(True)
plt.tight_layout()

# Save the figure
output_path = "/data/purdue-af-registered-users.png"
plt.savefig(output_path, dpi=300, bbox_inches='tight')
print(f"Plot saved as {output_path}") 