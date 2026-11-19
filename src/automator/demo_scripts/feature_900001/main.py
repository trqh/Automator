"""Offline demonstration: creates a SQLite result using Python's standard library."""
import sqlite3
from pathlib import Path

output = Path(__file__).resolve().parent / "demo_success.sqlite"
with sqlite3.connect(output) as connection:
    connection.execute("CREATE TABLE IF NOT EXISTS demo (label TEXT NOT NULL)")
    connection.execute("INSERT INTO demo(label) VALUES (?)", ("Automator demo completed",))
print("Automator demo completed; SQLite result written.")
