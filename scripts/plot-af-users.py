import sqlite3
import pandas as pd
import matplotlib.pyplot as plt
import mplhep as hep

# Apply the CMS style from mplhep
hep.style.use(hep.style.CMS)

# Connect to the JupyterHub SQLite database
conn = sqlite3.connect('jupyterhub.sqlite')

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
df['reg_date'] = pd.to_datetime(df['reg_date'])
df['cumulative_users'] = df['num_users'].cumsum()

# Create the cumulative plot using mplhep's CMS style
plt.figure(figsize=(12, 9))
plt.plot(df['reg_date'], df['cumulative_users'], marker='o', linestyle='-')

# Set labels and title as specified
plt.xlabel("Date")
plt.ylabel("Registered Users")
plt.title("Purdue Analysis Facility - Registered Users")
plt.xticks(rotation=35)
plt.ylim(0, 250)
plt.grid(True)
plt.tight_layout()

# Save the figure
plt.savefig("purdue-af-registered-users.png")
print("Plot saved as purdue-af-registered-users.png")