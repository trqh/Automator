from pathlib import Path

import pytest

from automator.infrastructure.storage_paths import StoragePaths, legacy_storage


def test_legacy_paths_preserve_current_behavior(tmp_path):
    paths = legacy_storage(tmp_path)
    assert paths.path("work_ledger") == tmp_path / "logs/work_tracking.sqlite3"
    assert paths.path("run_history") == tmp_path / "run_history.jsonl"
    assert paths.path("sessions") == tmp_path / "logs/sessions"
    assert paths.path("feature_runs") == tmp_path / "logs"
    assert paths.path("clickup_last_run") == tmp_path / "logs/clickup_task_id_sync_last_run.json"


def test_organized_layout_categories(tmp_path):
    paths = StoragePaths(tmp_path, "organized")
    expected = {
        "work_ledger": "logs/meta/work_tracking.sqlite3",
        "clickup_state": "logs/meta/clickup_sync_state.json",
        "batch_metadata": "logs/meta/batch_metadata.json",
        "clickup_last_run": "logs/reports/sync/clickup_task_id_sync_last_run.json",
        "reconciliation": "logs/reports/reconciliation/workday_reconciliation.json",
        "structured_events": "logs/runs/events/structured_events.jsonl",
        "gui_actions": "logs/runs/gui/gui_actions.jsonl",
        "run_history": "logs/runs/features/run_history.jsonl",
        "sessions": "logs/runs/sessions",
        "feature_runs": "logs/runs/features",
    }
    for key, relative in expected.items():
        assert paths.path(key) == tmp_path / relative


def test_import_and_resolution_do_not_create_directories(tmp_path):
    paths = StoragePaths(tmp_path / "not-created", "organized")
    assert paths.path("work_ledger").name == "work_tracking.sqlite3"
    assert not paths.root.exists()


def test_directory_creation_is_idempotent_and_preserves_data(tmp_path):
    paths = StoragePaths(tmp_path, "organized")
    paths.ensure_directories()
    ledger = paths.path("work_ledger")
    ledger.write_bytes(b"existing-ledger-test-bytes")
    paths.ensure_directories()
    assert ledger.read_bytes() == b"existing-ledger-test-bytes"
    assert (tmp_path / "logs/archive/notes").is_dir()
    assert (tmp_path / "logs/reports/batching").is_dir()


def test_legacy_setup_creates_no_organized_directories(tmp_path):
    paths = legacy_storage(tmp_path)
    paths.ensure_directories()
    assert (tmp_path / "logs").is_dir()
    assert not (tmp_path / "logs/meta").exists()


def test_migration_manifest_is_declarative_only(tmp_path):
    paths = legacy_storage(tmp_path)
    mapping = dict(paths.migration_pairs())
    assert mapping[tmp_path / "logs/work_tracking.sqlite3"] == tmp_path / "logs/meta/work_tracking.sqlite3"
    assert mapping[tmp_path / "logs/sessions"] == tmp_path / "logs/runs/sessions"
    assert tmp_path / "logs" not in mapping
    assert not (tmp_path / "logs").exists()


def test_unknown_paths_and_layout_fail_closed(tmp_path):
    with pytest.raises(ValueError):
        StoragePaths(tmp_path, "unknown")
    with pytest.raises(KeyError):
        legacy_storage(tmp_path).path("arbitrary")
