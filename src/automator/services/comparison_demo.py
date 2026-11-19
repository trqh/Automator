"""Fully isolated synthetic canonical comparison demonstration.

Never loads real records or uses a user's feature workspace. All files live
in a unique run folder under the writable application storage root.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ..workers.sqlite_compare_worker import (
    ComparisonResult, compare_feature, safe_replace_from_candidate,
)
from contextlib import closing

DEMO_FEATURE = '900002'


@dataclass(frozen=True)
class ComparisonDemo:
    run_directory: Path
    workspace: Path
    candidate: Path
    canonical: Path
    initial_result: ComparisonResult


def _database(path: Path, rows: list[tuple[int, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The database is created only inside a new UUID-named demo directory.
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.executescript('''
                PRAGMA foreign_keys = ON;
                CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE identifiers (
                    id INTEGER PRIMARY KEY,
                    person_id INTEGER NOT NULL REFERENCES person(id),
                    value TEXT NOT NULL UNIQUE
                );
            ''')
            connection.executemany('INSERT INTO person(id, name) VALUES (?, ?)',
                                   [(id_, name) for id_, name, _ in rows])
            connection.executemany('INSERT INTO identifiers(person_id, value) VALUES (?, ?)',
                                   [(id_, identifier) for id_, _, identifier in rows])


def prepare_comparison_demo(data_root: Path) -> ComparisonDemo:
    """Create a baseline and altered candidate, then compare without accepting."""
    root = Path(data_root).expanduser().resolve() / 'demo_runs'
    run_dir = root / (datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                      + '_comparison_' + uuid4().hex)
    run_dir.mkdir(parents=True, exist_ok=False)
    workspace = run_dir / 'workspace'
    candidate = workspace / ('feature_' + DEMO_FEATURE) / (DEMO_FEATURE + '.sqlite')
    canonical = run_dir / 'Canonical Outputs' / (DEMO_FEATURE + '.sqlite')
    baseline_rows = [(1, 'Alex Morgan', 'DEMO-A'), (2, 'Jordan Lee', 'DEMO-B')]
    updated_rows = [(17, 'Alex Morgan Updated', 'DEMO-A'),
                    (18, 'Casey Brooks', 'DEMO-C')]
    _database(canonical, baseline_rows)
    _database(candidate, updated_rows)
    comparison = compare_feature(DEMO_FEATURE, workspace, canonical.parent)
    if comparison.state != 'DIFFERENT':
        raise RuntimeError('Synthetic demo comparison must report DIFFERENT: '
                           + comparison.state + ': ' + comparison.message)
    return ComparisonDemo(run_dir, workspace, candidate, canonical, comparison)


def accept_comparison_demo(demo: ComparisonDemo) -> ComparisonResult:
    """Call only after explicit user confirmation, then verify SAME."""
    if demo.initial_result.state != 'DIFFERENT':
        raise ValueError('Only a differing comparison demo can be accepted')
    safe_replace_from_candidate(demo.candidate, demo.canonical)
    result = compare_feature(DEMO_FEATURE, demo.workspace, demo.canonical.parent)
    if result.state != 'SAME':
        raise RuntimeError('Accepted demo canonical did not compare SAME: '
                           + result.state + ': ' + result.message)
    return result
