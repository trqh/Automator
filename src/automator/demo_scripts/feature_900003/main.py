"""Deterministic fictional-person example for Automator's SQLite comparator.

This script is designed to be copied to feature_900003/main.py and run by
Automator's normal feature execution worker. It uses only Python's stdlib.
All names and identifiers are fictional demonstration data.
"""

import os
import sqlite3
import tempfile
from pathlib import Path
from contextlib import closing
# Change this to False, then rerun to demonstrate a detected new person.
BASELINE_ONLY = True

FEATURE_NUMBER = "900003"
PEOPLE = (
    (1, "Alex Morgan", "DEMO-001"),
    (2, "Casey Brooks", "DEMO-002"),
    (3, "Jordan Lee", "DEMO-003"),
)
EXTRA_PERSON = (4, "Riley Parker", "DEMO-004")


def generate_database(output: Path, *, baseline_only: bool) -> None:
    """Replace the candidate using a complete, fresh SQLite file.

    The temporary file is created beside the output, so os.replace is atomic
    on supported local filesystems. A failed build leaves the prior candidate
    intact. Canonical files and history are never touched by this script.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = PEOPLE if baseline_only else PEOPLE + (EXTRA_PERSON,)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".900003_", suffix=".sqlite", dir=str(output.parent)
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript("""
                CREATE TABLE person (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE identifiers (
                    id INTEGER PRIMARY KEY,
                    person_id INTEGER NOT NULL REFERENCES person(id),
                    value TEXT NOT NULL UNIQUE
                );
            """)
            connection.executemany(
                "INSERT INTO person(id, name) VALUES (?, ?)",
                ((person_id, name) for person_id, name, _ in rows),
            )
            connection.executemany(
                "INSERT INTO identifiers(id, person_id, value) VALUES (?, ?, ?)",
                ((person_id, person_id, identifier) for person_id, _, identifier in rows),
            )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    output = Path(__file__).resolve().parent / f"{FEATURE_NUMBER}.sqlite"
    generate_database(output, baseline_only=BASELINE_ONLY)
    print(f"Feature {FEATURE_NUMBER}: created {output.name} with "
          f"{3 if BASELINE_ONLY else 4} fictional people")


if __name__ == "__main__":
    main()
