
from datetime import datetime

from automator.ui.feature_manager_gui import (
    clickup_daily_sync_report_is_current,
    clickup_daily_sync_is_ready,
    clickup_reloaded_records_match
)
from types import SimpleNamespace



def test_daily_sync_requires_success_and_today():
    today = "2026-11-14"
    assert clickup_daily_sync_is_ready(
        {"date": today, "status": "SYNCED"}, today
    )
    assert not clickup_daily_sync_is_ready(
        {"date": "2026-11-13", "status": "SYNCED"}, today
    )
    assert not clickup_daily_sync_is_ready(
        {"date": today, "status": "FAILED"}, today
    )
    assert not clickup_daily_sync_is_ready({}, today)


def test_report_must_belong_to_current_session():
    session_id = "2026-11-14_10-47-14_Sync_ClickUp_Task_IDs"
    report = {
        "ok": True,
        "started_at": "2026-11-14T10:47:15",
    }

    assert clickup_daily_sync_report_is_current(
        report, session_id, "2026-11-14"
    )

    assert not clickup_daily_sync_report_is_current(
        {**report, "ok": False}, session_id, "2026-11-14"
    )
    assert not clickup_daily_sync_report_is_current(
        {**report, "started_at": "2026-11-14T10:46:59"},
        session_id,
        "2026-11-14",
    )
    assert not clickup_daily_sync_report_is_current(
        report, session_id, "2026-11-15"
    )
    assert not clickup_daily_sync_report_is_current(
        {"ok": True}, session_id, "2026-11-14"
    )


def test_invalid_report_and_session_fail_closed():
    assert not clickup_daily_sync_report_is_current(
        {"ok": True, "started_at": "invalid"},
        "invalid-session",
        "2026-11-14",
    )





def _gui_record(feature, status="Analyzed"):
    return SimpleNamespace(
        feature_number=feature,
        status=status,
        frequency="weekly",
        clickup_status="In Progress",
        clickup_task_id="task-1",
        notes="Original note",
    )


def _disk_record(feature, status="Analyzed"):
    return {
        "feature_number": feature,
        "status": status,
        "frequency": "Weekly",
        "clickup_status": "In Progress",
        "clickup_task_id": "task-1",
        "notes": "Original note",
    }


def test_reloaded_records_match_disk():
    assert clickup_reloaded_records_match(
        [_gui_record("123")],
        [_disk_record("123")],
    )


def test_reload_rejects_wrong_feature_even_with_same_record_count():
    assert not clickup_reloaded_records_match(
        [_gui_record("456")],
        [_disk_record("123")],
    )


def test_reload_rejects_stale_status():
    assert not clickup_reloaded_records_match(
        [_gui_record("123", "Analyzed")],
        [_disk_record("123", "Prepare for upload")],
    )


def test_reload_rejects_duplicate_feature_numbers():
    assert not clickup_reloaded_records_match(
        [_gui_record("123"), _gui_record("123")],
        [_disk_record("123"), _disk_record("123")],
    )


def test_reload_rejects_changed_clickup_identity():
    disk = _disk_record("123")
    disk["clickup_task_id"] = "different-task"

    assert not clickup_reloaded_records_match(
        [_gui_record("123")],
        [disk],
    )

def test_reload_rejects_stale_notes():
    gui = _gui_record("123")
    gui.notes = "Original note"

    disk = _disk_record("123")
    disk["notes"] = (
        "Ignore. This task has been removed from the list\n"
        "Original note"
    )

    assert not clickup_reloaded_records_match(
        [gui],
        [disk],
    )