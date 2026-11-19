"""Stage E: real first-run -> normal worker -> SQLite canonical lifecycle."""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from automator.infrastructure.runtime_config import RuntimeSettings
from automator.services.first_run import initialize_first_run
from automator.services.trusted_example import is_trusted_example
from automator.workers.feature_setup_worker import inspect_feature_setup
from automator.workers.run_features_worker import run_features
from automator.workers.sqlite_compare_worker import (
    compare_feature,
    inspect_sqlite_differences,
    safe_replace_from_candidate,
)


def _run(settings: RuntimeSettings, workspace: Path):
    # Emulate a frozen distribution with bundled Python, invoking normal worker.
    results = run_features(
        features=["900003"],
        script_runs_dir=workspace,
        logs_dir=settings.data_root / "logs",
        runtime_settings=settings,
        max_workers=1,
        frozen=True,
        bundled_python=Path(sys.executable),
    )
    assert len(results) == 1
    assert results[0].status == "success", results


def _ids(database: Path):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(
            "SELECT value FROM identifiers ORDER BY person_id"
        ).fetchall()


def test_first_run_normal_worker_and_full_canonical_history(tmp_path):
    settings = RuntimeSettings(data_root=tmp_path / "isolated user data")
    initial = initialize_first_run(settings)
    workspace = initial.workspace_path
    records = json.loads(initial.records_path.read_text(encoding="utf-8"))
    assert [r["feature_number"] for r in records] == ["900003"]
    assert inspect_feature_setup("900003", workspace).state == "READY"
    script = workspace / "feature_900003" / "main.py"
    candidate = workspace / "feature_900003" / "900003.sqlite"
    canonical = settings.data_root / "Canonical Outputs" / "900003.sqlite"
    canonical_dir = canonical.parent
    history = settings.data_root / "Canonical Output History" / "900003"

    assert compare_feature("900003", workspace, canonical_dir).state == "NO CANDIDATE"
    assert is_trusted_example("900003", workspace)
    _run(settings, workspace)
    assert _ids(candidate) == [("DEMO-001",), ("DEMO-002",), ("DEMO-003",)]
    assert compare_feature("900003", workspace, canonical_dir).state == "NO CANONICAL"
    safe_replace_from_candidate(candidate, canonical)
    assert compare_feature("900003", workspace, canonical_dir).state == "SAME"

    source = script.read_bytes()
    assert source.count(b"BASELINE_ONLY = True") == 1
    script.write_bytes(source.replace(b"BASELINE_ONLY = True", b"BASELINE_ONLY = False", 1))
    assert is_trusted_example("900003", workspace)
    _run(settings, workspace)
    diff = compare_feature("900003", workspace, canonical_dir)
    assert diff.state == "DIFFERENT"
    assert diff.summary and "DEMO-004" in diff.summary.added_identifiers
    details = inspect_sqlite_differences(candidate, canonical)
    assert any(item.state != "SAME" for item in details.table_differences)

    original = canonical.read_bytes()
    safe_replace_from_candidate(candidate, canonical)
    archives = list(history.glob("*.sqlite"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == original
    assert _ids(archives[0]) == [("DEMO-001",), ("DEMO-002",), ("DEMO-003",)]
    assert _ids(canonical)[-1] == ("DEMO-004",)
    assert compare_feature("900003", workspace, canonical_dir).state == "SAME"

    _run(settings, workspace)
    assert compare_feature("900003", workspace, canonical_dir).state == "SAME"
    assert len(list(history.glob("*.sqlite"))) == 1


def test_relaunch_preserves_accepted_canonical_and_user_toggle(tmp_path):
    settings = RuntimeSettings(data_root=tmp_path / "second isolated user data")
    first = initialize_first_run(settings)
    workspace = first.workspace_path
    script = workspace / "feature_900003" / "main.py"
    _run(settings, workspace)
    candidate = workspace / "feature_900003" / "900003.sqlite"
    canonical_dir = settings.data_root / "Canonical Outputs"
    safe_replace_from_candidate(candidate, canonical_dir / "900003.sqlite")
    script.write_bytes(script.read_bytes().replace(b"BASELINE_ONLY = True", b"BASELINE_ONLY = False", 1))
    before_records = first.records_path.read_bytes()
    second = initialize_first_run(settings)
    assert second.created == ()
    assert first.records_path.read_bytes() == before_records
    assert b"BASELINE_ONLY = False" in script.read_bytes()
    assert compare_feature("900003", workspace, canonical_dir).state == "SAME"
    _run(settings, workspace)
    assert compare_feature("900003", workspace, canonical_dir).state == "DIFFERENT"
