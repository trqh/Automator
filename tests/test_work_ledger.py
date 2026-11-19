import datetime as dt
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from automator.services.work_ledger import (
    ActiveIntervalError,
    IntervalOverlapError,
    NotFoundError,
    RevisionConflictError,
    SchemaVersionError,
    ValidationError,
    WorkLedger,
    to_unix_milliseconds,
)


UTC = dt.timezone.utc


def instant(hour, minute=0):
    return dt.datetime(2026, 10, 15, hour, minute, tzinfo=UTC)


def ledger(tmp_path):
    return WorkLedger(tmp_path / "work_tracking.sqlite3", clock=lambda: instant(12))


def test_unix_millisecond_conversion_uses_integer_precision():
    before_epoch = dt.datetime(1969, 12, 31, 23, 59, 59, 999_500, tzinfo=UTC)
    after_epoch = dt.datetime(1970, 1, 1, 0, 0, 0, 1_999, tzinfo=UTC)
    assert to_unix_milliseconds(before_epoch) == -1
    assert to_unix_milliseconds(after_epoch) == 1


def test_start_is_immediately_durable_and_recovers_after_reopen(tmp_path):
    path = tmp_path / "work_tracking.sqlite3"
    started = WorkLedger(path, clock=lambda: instant(9)).start("29691", category="debug")

    recovered = WorkLedger(path, clock=lambda: instant(10)).active_intervals()

    assert [(item.id, item.feature_number, item.category) for item in recovered] == [
        (started.id, "29691", "debug")
    ]
    assert recovered[0].ended_at is None


def test_parallel_features_are_allowed_but_same_feature_cannot_start_twice(tmp_path):
    subject = ledger(tmp_path)
    subject.start("29691", started_at=instant(9))
    other = subject.start("30163", started_at=instant(9, 5))

    assert other.is_active
    with pytest.raises(ActiveIntervalError):
        subject.start("29691", started_at=instant(9, 10))


def test_stopped_feature_can_have_a_separate_sequential_interval(tmp_path):
    subject = ledger(tmp_path)
    first = subject.start("29691", started_at=instant(9))
    finished = subject.stop_interval(first.id, ended_at=instant(10))
    second = subject.start("29691", category="review", started_at=instant(11))

    assert finished.ended_at == instant(10)
    assert second.category == "review"
    assert [item.id for item in subject.list_intervals(feature_number="29691")] == [first.id, second.id]


def test_same_feature_overlap_is_rejected_but_cross_feature_overlap_is_preserved(tmp_path):
    subject = ledger(tmp_path)
    subject.add_interval("29691", started_at=instant(9), ended_at=instant(10))
    subject.add_interval("30163", started_at=instant(9, 30), ended_at=instant(10, 30))

    with pytest.raises(IntervalOverlapError):
        subject.add_interval("29691", started_at=instant(9, 30), ended_at=instant(10, 30))


def test_edit_delete_events_and_revisions_are_durable(tmp_path):
    subject = ledger(tmp_path)
    item = subject.add_interval("29691", started_at=instant(9), ended_at=instant(10), notes="inspect")
    edited = subject.update_interval(item.id, notes="review", expected_revision=item.revision)

    assert edited.notes == "review"
    assert edited.revision == 2
    with pytest.raises(RevisionConflictError):
        subject.update_interval(item.id, notes="stale", expected_revision=1)
    deleted = subject.delete_interval(item.id, expected_revision=2)

    assert deleted.deleted_at is not None
    assert subject.list_intervals() == []
    events = subject.events(item.id)
    assert [event["event_type"] for event in events] == ["added", "updated", "deleted"]
    updated_payload = json.loads(events[1]["payload_json"])
    assert updated_payload["before"]["notes"] == "inspect"
    assert updated_payload["after"]["notes"] == "review"


def test_categories_are_seeded_and_can_be_extended(tmp_path):
    subject = ledger(tmp_path)
    assert "development" in subject.list_categories()
    subject.add_category("research")
    item = subject.add_interval("29691", category="research", started_at=instant(9), ended_at=instant(10))

    assert item.category == "research"


def test_daily_totals_report_recorded_covered_and_parallel_overlap(tmp_path):
    subject = ledger(tmp_path)
    subject.add_interval("29691", started_at=instant(9), ended_at=instant(11))
    subject.add_interval("30163", started_at=instant(10), ended_at=instant(12))
    subject.add_interval("53362", started_at=instant(13), ended_at=instant(14))

    totals = subject.daily_totals(dt.date(2026, 10, 15), as_of=instant(23, 59))

    assert totals.recorded_us == 5 * 60 * 60 * 1_000_000
    assert totals.covered_us == 4 * 60 * 60 * 1_000_000
    assert totals.overlap_us == 1 * 60 * 60 * 1_000_000


def test_stop_requires_an_active_interval(tmp_path):
    with pytest.raises(NotFoundError):
        ledger(tmp_path).stop("29691")


def test_feature_numbers_are_ascii_digits_and_categories_normalize_case(tmp_path):
    subject = ledger(tmp_path)
    subject.add_category("Research")
    item = subject.add_interval("0029691", category="RESEARCH", started_at=instant(9), ended_at=instant(10))

    assert item.feature_number == "0029691"
    assert item.category == "research"
    assert subject.list_categories().count("work") == 1
    subject.add_category("Work")
    assert subject.list_categories().count("work") == 1
    with pytest.raises(ValidationError):
        subject.start("feature-29691", started_at=instant(10))


def test_daily_totals_use_configured_local_billing_day_across_utc_midnight(tmp_path):
    amman = ZoneInfo("Asia/Amman")
    subject = WorkLedger(
        tmp_path / "work_tracking.sqlite3",
        clock=lambda: dt.datetime(2026, 10, 15, 23, tzinfo=UTC),
        billing_timezone=amman,
    )
    subject.add_interval(
        "29691",
        started_at=dt.datetime(2026, 10, 14, 20, 30, tzinfo=UTC),
        ended_at=dt.datetime(2026, 10, 14, 22, tzinfo=UTC),
    )

    totals = subject.daily_totals(dt.date(2026, 10, 15), as_of=dt.datetime(2026, 10, 15, 23, tzinfo=UTC))

    assert totals.recorded_us == 60 * 60 * 1_000_000
    assert totals.covered_us == totals.recorded_us


def test_failed_overlap_edit_rolls_back_interval_and_event_changes(tmp_path):
    subject = ledger(tmp_path)
    subject.add_interval("29691", started_at=instant(9), ended_at=instant(10))
    second = subject.add_interval("29691", started_at=instant(10), ended_at=instant(11), notes="safe")

    with pytest.raises(IntervalOverlapError):
        subject.update_interval(second.id, started_at=instant(9, 30), notes="must not persist")

    persisted = subject.get_interval(second.id)
    assert persisted.started_at == instant(10)
    assert persisted.notes == "safe"
    assert persisted.revision == 1
    assert [event["event_type"] for event in subject.events(second.id)] == ["added"]


def test_migrates_legacy_category_case_without_losing_interval_rows(tmp_path):
    path = tmp_path / "work_tracking.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE categories (name TEXT PRIMARY KEY, created_at_us INTEGER NOT NULL, retired_at_us INTEGER);
        CREATE TABLE intervals (
            id TEXT PRIMARY KEY, feature_number TEXT NOT NULL, category TEXT NOT NULL REFERENCES categories(name),
            started_at_us INTEGER NOT NULL, ended_at_us INTEGER, notes TEXT NOT NULL DEFAULT '',
            clickup_task_id TEXT, clickup_time_entry_id TEXT, sync_status TEXT NOT NULL DEFAULT 'pending',
            sync_error TEXT, session_id TEXT, correlation_id TEXT, created_at_us INTEGER NOT NULL,
            updated_at_us INTEGER NOT NULL, deleted_at_us INTEGER, revision INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO categories VALUES ('Work', 1, NULL);
        INSERT INTO intervals VALUES (
            'legacy-id', '29691', 'Work', 1760518800000000, 1760522400000000, '', NULL, NULL,
            'pending', NULL, NULL, NULL, 1760518800000000, 1760518800000000, NULL, 1
        );
        """
    )
    connection.commit()
    connection.close()

    subject = WorkLedger(path, clock=lambda: instant(12))

    assert subject.get_interval("legacy-id").category == "work"
    assert "work" in subject.list_categories()
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 3


def test_rejects_database_schema_newer_than_this_ledger(tmp_path):
    path = tmp_path / "work_tracking.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 4")

    with pytest.raises(SchemaVersionError):
        WorkLedger(path)


def test_time_sync_state_is_audited_and_synced_intervals_are_immutable(tmp_path):
    subject = WorkLedger(tmp_path / "work_tracking.sqlite3", clock=lambda: instant(12))
    interval = subject.add_interval("29691", category="development", started_at=instant(9), ended_at=instant(10))
    token, reconcile_only = subject.claim_time_sync(interval.id, "task-1")
    assert reconcile_only is False
    subject.mark_time_submission_started(interval.id, token)
    synced = subject.finish_time_sync(interval.id, token, "task-1", "entry-1")
    assert synced.sync_status == "synced"
    assert synced.clickup_task_id == "task-1"
    assert synced.clickup_time_entry_id == "entry-1"
    assert subject.events(interval.id)[-1]["event_type"] == "time_sync_synced"
    with pytest.raises(ValidationError, match="submitted or in-flight"):
        subject.update_interval(interval.id, notes="changed")
    with pytest.raises(ValidationError, match="submitted or in-flight"):
        subject.delete_interval(interval.id)


def test_editing_failed_interval_clears_sync_error_and_returns_it_to_pending(tmp_path):
    subject = WorkLedger(tmp_path / "work_tracking.sqlite3", clock=lambda: instant(12))
    interval = subject.add_interval("29691", category="work", started_at=instant(9), ended_at=instant(10))
    failed = subject.record_time_sync(interval.id, status="failed", error="temporary network error")
    assert failed.sync_status == "failed"
    updated = subject.update_interval(interval.id, notes="corrected interval")
    assert updated.sync_status == "pending"
    assert updated.sync_error is None


def test_uncertain_time_entry_submission_locks_edits_and_can_only_reconcile(tmp_path):
    subject = WorkLedger(tmp_path / "work_tracking.sqlite3", clock=lambda: instant(12))
    interval = subject.add_interval("29691", category="work", started_at=instant(9), ended_at=instant(10))
    token, _ = subject.claim_time_sync(interval.id, "task-1")
    subject.mark_time_submission_started(interval.id, token)
    subject.fail_time_sync(interval.id, token, "response timed out", uncertain=True)
    assert subject.get_interval(interval.id).sync_status == "unknown"
    with pytest.raises(ValidationError, match="submitted or in-flight"):
        subject.update_interval(interval.id, notes="changed")
    retry_token, reconcile_only = subject.claim_time_sync(interval.id, "task-1")
    assert reconcile_only is True
    retry = subject.finish_time_sync(interval.id, retry_token, "task-1", "entry-1")
    assert retry.sync_status == "synced"
    assert retry.clickup_time_entry_id == "entry-1"


def test_unknown_time_entry_without_readback_stays_locked_until_manual_release(tmp_path):
    subject = ledger(tmp_path)
    interval = subject.add_interval("29691", category="work", started_at=instant(9), ended_at=instant(10))
    token, _ = subject.claim_time_sync(interval.id, "task-1")
    subject.mark_time_submission_started(interval.id, token)
    subject.fail_time_sync(interval.id, token, "readback unavailable", uncertain=True)
    reconcile_token, reconcile_only = subject.claim_time_sync(interval.id, "task-1")
    assert reconcile_only is True
    subject.fail_time_sync(interval.id, reconcile_token, "still not visible", uncertain=True)
    assert subject.get_interval(interval.id).sync_status == "unknown"
    with pytest.raises(ValidationError, match="Manual confirmation"):
        subject.release_sync_for_manual_reconciliation(interval.id, confirmed_no_request_pending=False)
    released = subject.release_sync_for_manual_reconciliation(interval.id, confirmed_no_request_pending=True)
    assert released.sync_status == "pending"


def test_atomic_time_sync_claim_allows_only_one_submitter(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    subject = WorkLedger(tmp_path / "work_tracking.sqlite3", clock=lambda: instant(12))
    interval = subject.add_interval("29691", category="work", started_at=instant(9), ended_at=instant(10))
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        try:
            return subject.claim_time_sync(interval.id, "task-1")
        except ValidationError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _x: claim(), range(2)))
    accepted = [item for item in results if item is not None]
    assert len(accepted) == 1
    assert accepted[0][1] is False


def test_synced_time_entry_is_terminal_and_entry_id_cannot_be_replaced(tmp_path):
    subject = WorkLedger(tmp_path / "work_tracking.sqlite3", clock=lambda: instant(12))
    interval = subject.add_interval("29691", category="work", started_at=instant(9), ended_at=instant(10))
    token, _ = subject.claim_time_sync(interval.id, "task-1")
    subject.finish_time_sync(interval.id, token, "task-1", "entry-1")
    with pytest.raises(ValidationError, match="already synced"):
        subject.claim_time_sync(interval.id, "task-1")
    with pytest.raises(ValidationError, match="submitted or uncertain"):
        subject.record_time_sync(interval.id, status="failed", error="regress")


def test_day_closure_reopens_when_ledger_interval_changes_and_clips_cross_midnight(tmp_path):
    zone = dt.timezone(dt.timedelta(hours=3))
    subject = WorkLedger(tmp_path / "work_tracking.sqlite3", clock=lambda: instant(12), billing_timezone=zone)
    day = dt.date(2026, 1, 1)
    interval = subject.add_interval(
        "29691", category="work",
        started_at=dt.datetime(2026, 1, 1, 23, 0, tzinfo=zone),
        ended_at=dt.datetime(2026, 1, 2, 2, 0, tzinfo=zone),
    )
    assert subject.daily_totals(day).recorded_seconds == 3_600
    token, _ = subject.claim_time_sync(interval.id, "task-1")
    subject.finish_time_sync(interval.id, token, "task-1", "entry-1")
    subject.close_day(day, {"status": "CLOSED"})
    assert subject.is_day_closed(day)
    subject.add_interval(
        "99999", category="work",
        started_at=dt.datetime(2026, 1, 1, 12, 0, tzinfo=zone),
        ended_at=dt.datetime(2026, 1, 1, 13, 0, tzinfo=zone),
    )
    assert subject.is_day_closed(day) is False
