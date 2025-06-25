#!/usr/bin/env python3
import sqlite3

conn = sqlite3.connect("jupyterhub.sqlite")
cursor = conn.cursor()

# List all tables in the database.
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()

print("Tables in the database:")
for table in tables:
    print(" -", table[0])

# Display the first 5 rows of each table.
for table in tables:
    table_name = table[0]
    print(f"\nContents of table '{table_name}':")
    cursor.execute(f"SELECT * FROM {table_name} LIMIT 5;")
    rows = cursor.fetchall()
    for row in rows:
        print(row)

conn.close()
