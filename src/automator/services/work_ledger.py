"""Durable local ledger for manually recorded feature work intervals."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Sequence, Union


UTC = dt.timezone.utc
SCHEMA_VERSION = 3
_MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807
_MIN_DATETIME_US = -62_135_596_800_000_000
_MAX_DATETIME_US = 253_402_300_799_999_999
DEFAULT_CATEGORIES = (
    "development",
    "debug",
    "review",
    "comparison",
    "upload",
    "admin",
    "work",
)


class LedgerError(Exception):
    """Base exception for ledger operations."""


class ValidationError(LedgerError):
    """Raised when an interval value cannot represent valid work."""


class NotFoundError(LedgerError):
    """Raised when a requested live interval does not exist."""


class ActiveIntervalError(LedgerError):
    """Raised when a feature already has an active interval."""


class IntervalOverlapError(LedgerError):
    """Raised when intervals for one feature overlap."""


class RevisionConflictError(LedgerError):
    """Raised when an update is based on an obsolete interval revision."""


class SchemaVersionError(LedgerError):
    """Raised when a database requires a newer ledger implementation."""


Instant = Union[dt.datetime, int]


@dataclass(frozen=True)
class WorkInterval:
    """One manually recorded local interval. All instants use UTC."""

    id: str
    feature_number: str
    category: str
    started_at: dt.datetime
    ended_at: Optional[dt.datetime]
    notes: str
    clickup_task_id: Optional[str]
    clickup_time_entry_id: Optional[str]
    sync_status: str
    sync_error: Optional[str]
    session_id: Optional[str]
    correlation_id: Optional[str]
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: Optional[dt.datetime]
    revision: int

    @property
    def is_active(self) -> bool:
        return self.ended_at is None and self.deleted_at is None


@dataclass(frozen=True)
class DailyTotals:
    """Duration totals for one local billing day, without changing intervals."""

    day: dt.date
    recorded_us: int
    covered_us: int
    overlap_us: int

    @property
    def recorded_seconds(self) -> float:
        return self.recorded_us / 1_000_000

    @property
    def covered_seconds(self) -> float:
        return self.covered_us / 1_000_000

    @property
    def overlap_seconds(self) -> float:
        return self.overlap_us / 1_000_000


def utc_now() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def to_utc_microseconds(value: Instant) -> int:
    """Convert an aware UTC instant, or an existing microsecond value, to integer UTC."""
    if isinstance(value, int) and not isinstance(value, bool):
        if value < _MIN_DATETIME_US or value > _MAX_DATETIME_US:
            raise ValidationError("UTC microseconds are outside the supported datetime range.")
        return value
    if not isinstance(value, dt.datetime):
        raise ValidationError("An instant must be an aware datetime or UTC microseconds.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("An instant must include a time zone.")
    try:
        value = value.astimezone(UTC)
    except OverflowError as error:
        raise ValidationError("Instant is outside the supported UTC range.") from error
    epoch = dt.datetime(1970, 1, 1, tzinfo=UTC)
    delta = value - epoch
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000) + delta.microseconds


def from_utc_microseconds(value: int) -> dt.datetime:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError("UTC microseconds must be an integer.")
    try:
        return dt.datetime(1970, 1, 1, tzinfo=UTC) + dt.timedelta(microseconds=value)
    except OverflowError as error:
        raise ValidationError("UTC microseconds are outside the supported datetime range.") from error


def to_unix_milliseconds(value: Instant) -> int:
    """Convert through integer microseconds to avoid float timestamp rounding."""
    return to_utc_microseconds(value) // 1_000


class WorkLedger:
    """SQLite-backed ledger with atomic mutations and durable work-timer starts."""

    def __init__(
        self,
        path: Union[str, Path],
        *,
        clock: Callable[[], dt.datetime] = utc_now,
        busy_timeout_ms: int = 5_000,
        billing_timezone: Optional[dt.tzinfo] = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        if not isinstance(busy_timeout_ms, int) or busy_timeout_ms < 0:
            raise ValidationError("Busy timeout must be a non-negative integer.")
        self._busy_timeout_ms = busy_timeout_ms
        self.billing_timezone = billing_timezone or dt.datetime.now().astimezone().tzinfo
        if self.billing_timezone is None:
            raise ValidationError("A local billing time zone is required.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = %d" % self._busy_timeout_ms)
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version > SCHEMA_VERSION:
                raise SchemaVersionError("Ledger database uses a newer schema version.")
            has_intervals = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'intervals'"
            ).fetchone() is not None
            if has_intervals and current_version < SCHEMA_VERSION:
                self._migrate_legacy_schema(connection)
                current_version = SCHEMA_VERSION
            connection.execute("BEGIN IMMEDIATE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS categories (
                    name TEXT PRIMARY KEY COLLATE NOCASE,
                    created_at_us INTEGER NOT NULL,
                    retired_at_us INTEGER
                );

                CREATE TABLE IF NOT EXISTS intervals (
                    id TEXT PRIMARY KEY,
                    feature_number TEXT NOT NULL CHECK(length(feature_number) > 0),
                    category TEXT NOT NULL REFERENCES categories(name),
                    started_at_us INTEGER NOT NULL,
                    ended_at_us INTEGER,
                    notes TEXT NOT NULL DEFAULT '',
                    clickup_task_id TEXT,
                    clickup_time_entry_id TEXT,
                    sync_status TEXT NOT NULL DEFAULT 'pending',
                    sync_error TEXT,
                    sync_attempt_token TEXT,
                    session_id TEXT,
                    correlation_id TEXT,
                    created_at_us INTEGER NOT NULL,
                    updated_at_us INTEGER NOT NULL,
                    deleted_at_us INTEGER,
                    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
                    CHECK(ended_at_us IS NULL OR ended_at_us > started_at_us)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_interval_per_feature
                    ON intervals(feature_number)
                    WHERE ended_at_us IS NULL AND deleted_at_us IS NULL;
                CREATE INDEX IF NOT EXISTS intervals_by_start
                    ON intervals(started_at_us) WHERE deleted_at_us IS NULL;
                CREATE INDEX IF NOT EXISTS intervals_by_feature_start
                    ON intervals(feature_number, started_at_us)
                    WHERE deleted_at_us IS NULL;

                CREATE TABLE IF NOT EXISTS interval_events (
                    id TEXT PRIMARY KEY,
                    interval_id TEXT NOT NULL REFERENCES intervals(id),
                    event_type TEXT NOT NULL,
                    occurred_at_us INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS interval_events_by_interval
                    ON interval_events(interval_id, occurred_at_us);

                CREATE TABLE IF NOT EXISTS workday_closures (
                    day TEXT PRIMARY KEY,
                    snapshot_sha256 TEXT NOT NULL,
                    reconciliation_json TEXT NOT NULL,
                    closed_at_us INTEGER NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS intervals_reject_insert_overlap
                BEFORE INSERT ON intervals
                WHEN NEW.deleted_at_us IS NULL AND EXISTS (
                    SELECT 1 FROM intervals AS old
                    WHERE old.feature_number = NEW.feature_number
                      AND old.deleted_at_us IS NULL
                      AND COALESCE(old.ended_at_us, 9223372036854775807) > NEW.started_at_us
                      AND COALESCE(NEW.ended_at_us, 9223372036854775807) > old.started_at_us
                )
                BEGIN
                    SELECT RAISE(ABORT, 'interval overlaps an existing interval for this feature');
                END;

                CREATE TRIGGER IF NOT EXISTS intervals_reject_update_overlap
                BEFORE UPDATE OF feature_number, started_at_us, ended_at_us, deleted_at_us ON intervals
                WHEN NEW.deleted_at_us IS NULL AND EXISTS (
                    SELECT 1 FROM intervals AS old
                    WHERE old.id <> NEW.id
                      AND old.feature_number = NEW.feature_number
                      AND old.deleted_at_us IS NULL
                      AND COALESCE(old.ended_at_us, 9223372036854775807) > NEW.started_at_us
                      AND COALESCE(NEW.ended_at_us, 9223372036854775807) > old.started_at_us
                )
                BEGIN
                    SELECT RAISE(ABORT, 'interval overlaps an existing interval for this feature');
                END;
                """
            )
            interval_columns = {row[1] for row in connection.execute("PRAGMA table_info(intervals)")}
            if "sync_attempt_token" not in interval_columns:
                connection.execute("ALTER TABLE intervals ADD COLUMN sync_attempt_token TEXT")
            now_us = self._now_us()
            connection.executemany(
                "INSERT OR IGNORE INTO categories(name, created_at_us) VALUES (?, ?)",
                [(category, now_us) for category in DEFAULT_CATEGORIES],
            )
            connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate_legacy_schema(self, connection: sqlite3.Connection) -> None:
        """Migrate the initial ledger schema without discarding interval history."""
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE categories_replacement (
                    name TEXT PRIMARY KEY COLLATE NOCASE,
                    created_at_us INTEGER NOT NULL,
                    retired_at_us INTEGER
                )"""
            )
            connection.execute(
                """INSERT INTO categories_replacement(name, created_at_us, retired_at_us)
                   SELECT lower(name), MIN(created_at_us),
                          CASE WHEN MAX(retired_at_us IS NULL) THEN NULL ELSE MAX(retired_at_us) END
                   FROM categories GROUP BY lower(name)"""
            )
            connection.execute("UPDATE intervals SET category = lower(category)")
            connection.execute("DROP TABLE categories")
            connection.execute("ALTER TABLE categories_replacement RENAME TO categories")
            connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def _now_us(self) -> int:
        return to_utc_microseconds(self._clock())

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _required_text(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("%s is required." % name)
        return value.strip()

    @classmethod
    def _feature_number(cls, value: str) -> str:
        value = cls._required_text(value, "Feature number")
        if not value.isascii() or not value.isdigit():
            raise ValidationError("Feature number must contain only ASCII digits.")
        return value

    @classmethod
    def _category(cls, value: str) -> str:
        value = cls._required_text(value, "Category")
        normalized = value.casefold()
        if not normalized:
            raise ValidationError("Category is required.")
        return normalized

    @staticmethod
    def _nullable_text(value: Optional[str], name: str) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError("%s must be text or None." % name)
        return value

    def list_categories(self, *, include_retired: bool = False) -> List[str]:
        query = "SELECT name FROM categories"
        if not include_retired:
            query += " WHERE retired_at_us IS NULL"
        query += " ORDER BY name"
        with self._read() as connection:
            return [row["name"] for row in connection.execute(query)]

    def add_category(self, name: str) -> None:
        name = self._category(name)
        with self._write() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO categories(name, created_at_us, retired_at_us) VALUES (?, ?, NULL)",
                (name, self._now_us()),
            )
            connection.execute("UPDATE categories SET retired_at_us = NULL WHERE name = ?", (name,))

    def retire_category(self, name: str) -> None:
        name = self._category(name)
        with self._write() as connection:
            result = connection.execute(
                "UPDATE categories SET retired_at_us = ? WHERE name = ? AND retired_at_us IS NULL",
                (self._now_us(), name),
            )
            if result.rowcount != 1:
                raise NotFoundError("Category does not exist or is already retired.")

    def start(
        self,
        feature_number: str,
        *,
        category: str = "work",
        notes: str = "",
        started_at: Optional[Instant] = None,
        clickup_task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> WorkInterval:
        return self.add_interval(
            feature_number,
            category=category,
            notes=notes,
            started_at=self._now_us() if started_at is None else started_at,
            ended_at=None,
            clickup_task_id=clickup_task_id,
            session_id=session_id,
            correlation_id=correlation_id,
            event_type="started",
        )

    def add_interval(
        self,
        feature_number: str,
        *,
        category: str = "work",
        notes: str = "",
        started_at: Instant,
        ended_at: Optional[Instant],
        clickup_task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        event_type: str = "added",
    ) -> WorkInterval:
        feature_number = self._feature_number(feature_number)
        category = self._category(category)
        if not isinstance(notes, str):
            raise ValidationError("Notes must be text.")
        started_us = to_utc_microseconds(started_at)
        ended_us = None if ended_at is None else to_utc_microseconds(ended_at)
        if ended_us is not None and ended_us <= started_us:
            raise ValidationError("End time must be after start time.")
        now_us = self._now_us()
        interval_id = str(uuid.uuid4())
        try:
            with self._write() as connection:
                self._ensure_active_category(connection, category)
                if ended_us is None and connection.execute(
                    "SELECT 1 FROM intervals WHERE feature_number = ? AND ended_at_us IS NULL AND deleted_at_us IS NULL",
                    (feature_number,),
                ).fetchone() is not None:
                    raise ActiveIntervalError("Feature already has an active interval.")
                connection.execute(
                    """INSERT INTO intervals (
                        id, feature_number, category, started_at_us, ended_at_us, notes,
                        clickup_task_id, session_id, correlation_id, created_at_us, updated_at_us
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        interval_id, feature_number, category, started_us, ended_us, notes,
                        self._nullable_text(clickup_task_id, "ClickUp task ID"),
                        self._nullable_text(session_id, "Session ID"),
                        self._nullable_text(correlation_id, "Correlation ID"), now_us, now_us,
                    ),
                )
                created = self._interval_by_id(connection, interval_id, include_deleted=True)
                self._append_event(connection, interval_id, event_type, now_us, 1, None, created)
                return created
        except sqlite3.IntegrityError as error:
            self._raise_integrity_error(error)
            raise AssertionError("unreachable")

    def stop(self, feature_number: str, *, ended_at: Optional[Instant] = None) -> WorkInterval:
        feature_number = self._feature_number(feature_number)
        now_us = self._now_us()
        ended_us = now_us if ended_at is None else to_utc_microseconds(ended_at)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM intervals WHERE feature_number = ? AND ended_at_us IS NULL AND deleted_at_us IS NULL",
                (feature_number,),
            ).fetchone()
            if row is None:
                raise NotFoundError("Feature has no active interval.")
            current = self._row_to_interval(row)
            if ended_us <= to_utc_microseconds(current.started_at):
                raise ValidationError("End time must be after start time.")
            revision = current.revision + 1
            connection.execute(
                "UPDATE intervals SET ended_at_us = ?, updated_at_us = ?, revision = ? WHERE id = ?",
                (ended_us, now_us, revision, current.id),
            )
            stopped = self._interval_by_id(connection, current.id, include_deleted=True)
            self._append_event(connection, current.id, "stopped", now_us, revision, current, stopped)
            return stopped

    def stop_interval(self, interval_id: str, *, ended_at: Optional[Instant] = None) -> WorkInterval:
        interval_id = self._required_text(interval_id, "Interval ID")
        now_us = self._now_us()
        ended_us = now_us if ended_at is None else to_utc_microseconds(ended_at)
        try:
            with self._write() as connection:
                current = self._interval_by_id(connection, interval_id)
                if not current.is_active:
                    raise ValidationError("Interval is not active.")
                if ended_us <= to_utc_microseconds(current.started_at):
                    raise ValidationError("End time must be after start time.")
                revision = current.revision + 1
                connection.execute(
                    "UPDATE intervals SET ended_at_us = ?, updated_at_us = ?, revision = ? WHERE id = ?",
                    (ended_us, now_us, revision, interval_id),
                )
                stopped = self._interval_by_id(connection, interval_id, include_deleted=True)
                self._append_event(connection, interval_id, "stopped", now_us, revision, current, stopped)
                return stopped
        except sqlite3.IntegrityError as error:
            self._raise_integrity_error(error)
            raise AssertionError("unreachable")

    def update_interval(
        self,
        interval_id: str,
        *,
        feature_number: Optional[str] = None,
        category: Optional[str] = None,
        notes: Optional[str] = None,
        started_at: Optional[Instant] = None,
        ended_at: Union[Optional[Instant], object] = ...,
        clickup_task_id: Union[Optional[str], object] = ...,
        expected_revision: Optional[int] = None,
    ) -> WorkInterval:
        interval_id = self._required_text(interval_id, "Interval ID")
        now_us = self._now_us()
        try:
            with self._write() as connection:
                current = self._interval_by_id(connection, interval_id)
                if current.sync_status in {"synced", "in_flight", "unknown", "reconciling"} or current.clickup_time_entry_id:
                    raise ValidationError("A submitted or in-flight interval cannot be edited without a ClickUp reconciliation workflow.")
                if expected_revision is not None and current.revision != expected_revision:
                    raise RevisionConflictError("Interval revision has changed.")
                next_feature = current.feature_number if feature_number is None else self._feature_number(feature_number)
                next_category = current.category if category is None else self._category(category)
                next_notes = current.notes if notes is None else self._nullable_text(notes, "Notes")
                next_start = to_utc_microseconds(current.started_at) if started_at is None else to_utc_microseconds(started_at)
                next_end = (None if current.ended_at is None else to_utc_microseconds(current.ended_at)) if ended_at is ... else (None if ended_at is None else to_utc_microseconds(ended_at))
                if next_end is not None and next_end <= next_start:
                    raise ValidationError("End time must be after start time.")
                next_task_id = current.clickup_task_id if clickup_task_id is ... else self._nullable_text(clickup_task_id, "ClickUp task ID")
                next_sync_status = "pending" if current.sync_status == "failed" else current.sync_status
                next_sync_error = None if current.sync_status == "failed" else current.sync_error
                self._ensure_active_category(connection, next_category)
                revision = current.revision + 1
                connection.execute(
                    """UPDATE intervals SET feature_number = ?, category = ?, notes = ?, started_at_us = ?,
                       ended_at_us = ?, clickup_task_id = ?, sync_status = ?, sync_error = ?, updated_at_us = ?, revision = ? WHERE id = ?""",
                    (next_feature, next_category, next_notes, next_start, next_end, next_task_id,
                     next_sync_status, next_sync_error, now_us, revision, interval_id),
                )
                updated = self._interval_by_id(connection, interval_id, include_deleted=True)
                self._append_event(connection, interval_id, "updated", now_us, revision, current, updated)
                return updated
        except sqlite3.IntegrityError as error:
            self._raise_integrity_error(error)
            raise AssertionError("unreachable")

    def delete_interval(self, interval_id: str, *, expected_revision: Optional[int] = None) -> WorkInterval:
        interval_id = self._required_text(interval_id, "Interval ID")
        now_us = self._now_us()
        with self._write() as connection:
            current = self._interval_by_id(connection, interval_id)
            if current.sync_status in {"synced", "in_flight", "unknown", "reconciling"} or current.clickup_time_entry_id:
                raise ValidationError("A submitted or in-flight interval cannot be deleted without a ClickUp reconciliation workflow.")
            if expected_revision is not None and current.revision != expected_revision:
                raise RevisionConflictError("Interval revision has changed.")
            revision = current.revision + 1
            connection.execute(
                "UPDATE intervals SET deleted_at_us = ?, updated_at_us = ?, revision = ? WHERE id = ?",
                (now_us, now_us, revision, interval_id),
            )
            deleted = self._interval_by_id(connection, interval_id, include_deleted=True)
            self._append_event(connection, interval_id, "deleted", now_us, revision, current, deleted)
            return deleted

    def record_time_sync(
        self,
        interval_id: str,
        *,
        status: str,
        clickup_task_id: Optional[str] = None,
        clickup_time_entry_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> WorkInterval:
        """Compatibility entry point restricted to safe local failure annotations."""
        if status != "failed":
            raise ValidationError("Use the atomic time-sync claim methods to change submission state.")
        current = self.get_interval(interval_id)
        if current.sync_status not in {"pending", "failed"}:
            raise ValidationError("A submitted or uncertain interval cannot be marked failed.")
        with self._write() as connection:
            current = self._interval_by_id(connection, interval_id)
            if current.sync_status not in {"pending", "failed"}:
                raise ValidationError("A submitted or uncertain interval cannot be marked failed.")
            return self._set_sync_state(connection, current, "failed", error=error,
                                         task_id=clickup_task_id, entry_id=None, token=None)

    def claim_time_sync(self, interval_id: str, task_id: str) -> tuple[str, bool]:
        """Atomically claim one interval; second result says submission is forbidden (reconcile only)."""
        token = str(uuid.uuid4())
        with self._write() as connection:
            current = self._interval_by_id(connection, interval_id)
            if current.ended_at is None or current.deleted_at is not None:
                raise ValidationError("Only completed, live intervals can be synchronized.")
            if current.sync_status == "synced":
                raise ValidationError("Interval is already synced.")
            if current.sync_status in {"reconciling", "in_flight"}:
                raise ValidationError("Another sync or unresolved submission already owns this interval.")
            if current.clickup_time_entry_id:
                raise ValidationError("Interval has a ClickUp entry ID and requires reconciliation.")
            reconcile_only = current.sync_status == "unknown"
            if current.sync_status not in {"pending", "failed", "unknown"}:
                raise ValidationError("Interval is not in a syncable state.")
            self._set_sync_state(connection, current, "reconciling", error=None,
                                  task_id=task_id, entry_id=None, token=token)
            return token, reconcile_only

    def mark_time_submission_started(self, interval_id: str, token: str) -> WorkInterval:
        with self._write() as connection:
            current = self._interval_by_id(connection, interval_id)
            self._require_sync_claim(current, token, "reconciling")
            return self._set_sync_state(connection, current, "in_flight", error=None,
                                        task_id=current.clickup_task_id, entry_id=None, token=token)

    def finish_time_sync(self, interval_id: str, token: str, task_id: str, entry_id: str) -> WorkInterval:
        task_id = self._required_text(task_id, "ClickUp task ID")
        entry_id = self._required_text(entry_id, "ClickUp time-entry ID")
        with self._write() as connection:
            current = self._interval_by_id(connection, interval_id)
            self._require_sync_claim(current, token, {"reconciling", "in_flight"})
            if current.ended_at is None or current.deleted_at is not None:
                raise ValidationError("Only completed, live intervals can be marked synced.")
            if current.clickup_task_id and current.clickup_task_id != task_id:
                raise ValidationError("ClickUp task ID cannot change after sync starts.")
            if current.clickup_time_entry_id and current.clickup_time_entry_id != entry_id:
                raise ValidationError("ClickUp time-entry ID cannot be replaced.")
            return self._set_sync_state(connection, current, "synced", error=None,
                                        task_id=task_id, entry_id=entry_id, token=None)

    def fail_time_sync(self, interval_id: str, token: str, error: str, *, uncertain: bool) -> WorkInterval:
        with self._write() as connection:
            current = self._interval_by_id(connection, interval_id)
            self._require_sync_claim(current, token, {"reconciling", "in_flight"})
            status = "unknown" if uncertain or current.sync_status == "in_flight" else "failed"
            return self._set_sync_state(connection, current, status, error=error,
                                        task_id=current.clickup_task_id, entry_id=None,
                                        token=token if status == "unknown" else None)

    def release_sync_for_manual_reconciliation(self, interval_id: str, *, confirmed_no_request_pending: bool) -> WorkInterval:
        """Release a crash-stale claim only after an operator confirms no request remains in progress."""
        if not confirmed_no_request_pending:
            raise ValidationError("Manual confirmation is required to release an uncertain sync.")
        with self._write() as connection:
            current = self._interval_by_id(connection, interval_id)
            if current.sync_status not in {"unknown", "reconciling", "in_flight"}:
                raise ValidationError("Interval has no unresolved time-sync claim.")
            return self._set_sync_state(connection, current, "pending",
                                        error="Manually released after confirming no ClickUp request or entry remains.",
                                        task_id=current.clickup_task_id, entry_id=None, token=None)

    def _require_sync_claim(self, current: WorkInterval, token: str, statuses) -> None:
        allowed = {statuses} if isinstance(statuses, str) else set(statuses)
        with self._read() as connection:
            row = connection.execute("SELECT sync_status, sync_attempt_token FROM intervals WHERE id = ?", (current.id,)).fetchone()
        if row is None or row["sync_status"] not in allowed or row["sync_attempt_token"] != token:
            raise RevisionConflictError("Time-sync claim is no longer current.")

    def _set_sync_state(self, connection, current, status, *, error, task_id, entry_id, token):
        revision = current.revision + 1
        next_task = current.clickup_task_id if task_id is None else self._nullable_text(task_id, "ClickUp task ID")
        next_entry = current.clickup_time_entry_id if entry_id is None else self._nullable_text(entry_id, "ClickUp time-entry ID")
        if current.clickup_time_entry_id and next_entry != current.clickup_time_entry_id:
            raise ValidationError("ClickUp time-entry ID cannot be replaced.")
        connection.execute(
            "UPDATE intervals SET clickup_task_id = ?, clickup_time_entry_id = ?, sync_status = ?, sync_error = ?, sync_attempt_token = ?, updated_at_us = ?, revision = ? WHERE id = ?",
            (next_task, next_entry, status, error, token, self._now_us(), revision, current.id),
        )
        updated = self._interval_by_id(connection, current.id, include_deleted=True)
        self._append_event(connection, current.id, "time_sync_%s" % status, self._now_us(), revision, current, updated)
        return updated

    def get_interval(self, interval_id: str, *, include_deleted: bool = False) -> WorkInterval:
        with self._read() as connection:
            return self._interval_by_id(connection, interval_id, include_deleted=include_deleted)

    def active_intervals(self) -> List[WorkInterval]:
        return self.list_intervals(active_only=True)

    def list_intervals(
        self,
        *,
        feature_number: Optional[str] = None,
        day: Optional[dt.date] = None,
        include_deleted: bool = False,
        active_only: bool = False,
    ) -> List[WorkInterval]:
        clauses = []
        values: List[object] = []
        if not include_deleted:
            clauses.append("deleted_at_us IS NULL")
        if feature_number is not None:
            clauses.append("feature_number = ?")
            values.append(self._feature_number(feature_number))
        if active_only:
            clauses.append("ended_at_us IS NULL")
        if day is not None:
            start_us, end_us = self._day_bounds(day)
            clauses.append("started_at_us < ? AND COALESCE(ended_at_us, ?) > ?")
            values.extend((end_us, _MAX_SQLITE_INTEGER, start_us))
        query = "SELECT * FROM intervals"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at_us, id"
        with self._read() as connection:
            return [self._row_to_interval(row) for row in connection.execute(query, values)]

    def events(self, interval_id: str) -> List[sqlite3.Row]:
        interval_id = self._required_text(interval_id, "Interval ID")
        with self._read() as connection:
            return list(connection.execute(
                "SELECT * FROM interval_events WHERE interval_id = ? ORDER BY revision, occurred_at_us, id", (interval_id,)
            ))

    def daily_totals(self, day: dt.date, *, as_of: Optional[Instant] = None) -> DailyTotals:
        start_us, end_us = self._day_bounds(day)
        end_cap = min(end_us, self._now_us() if as_of is None else to_utc_microseconds(as_of))
        if end_cap <= start_us:
            return DailyTotals(day=day, recorded_us=0, covered_us=0, overlap_us=0)
        spans = []
        for interval in self.list_intervals(day=day):
            interval_start = max(start_us, to_utc_microseconds(interval.started_at))
            interval_end = interval.ended_at
            interval_end_us = end_cap if interval_end is None else min(end_cap, to_utc_microseconds(interval_end))
            if interval_end_us > interval_start:
                spans.append((interval_start, interval_end_us))
        recorded = sum(end - start for start, end in spans)
        covered = self._covered_duration(spans)
        return DailyTotals(day=day, recorded_us=recorded, covered_us=covered, overlap_us=recorded - covered)

    def close_day(self, day: dt.date, reconciliation: dict) -> None:
        """Atomically close a fully synced day against the current interval snapshot."""
        if not isinstance(reconciliation, dict):
            raise ValidationError("Workday reconciliation must be an object.")
        start_us, end_us = self._day_bounds(day)
        with self._write() as connection:
            rows = list(connection.execute(
                "SELECT * FROM intervals WHERE deleted_at_us IS NULL AND started_at_us < ? AND COALESCE(ended_at_us, ?) > ? ORDER BY started_at_us, id",
                (end_us, _MAX_SQLITE_INTEGER, start_us),
            ))
            if any(row["ended_at_us"] is None for row in rows):
                raise ValidationError("Active intervals block workday closure.")
            if any(row["sync_status"] != "synced" for row in rows):
                raise ValidationError("Unsynced intervals block workday closure.")
            snapshot = self._closure_snapshot(rows)
            connection.execute(
                "INSERT INTO workday_closures(day, snapshot_sha256, reconciliation_json, closed_at_us) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET snapshot_sha256=excluded.snapshot_sha256, reconciliation_json=excluded.reconciliation_json, closed_at_us=excluded.closed_at_us",
                (day.isoformat(), snapshot, json.dumps(reconciliation, sort_keys=True), self._now_us()),
            )

    def is_day_closed(self, day: dt.date) -> bool:
        start_us, end_us = self._day_bounds(day)
        with self._read() as connection:
            saved = connection.execute("SELECT snapshot_sha256 FROM workday_closures WHERE day = ?", (day.isoformat(),)).fetchone()
            if saved is None:
                return False
            rows = list(connection.execute(
                "SELECT * FROM intervals WHERE deleted_at_us IS NULL AND started_at_us < ? AND COALESCE(ended_at_us, ?) > ? ORDER BY started_at_us, id",
                (end_us, _MAX_SQLITE_INTEGER, start_us),
            ))
            return all(row["ended_at_us"] is not None and row["sync_status"] == "synced" for row in rows) and saved["snapshot_sha256"] == self._closure_snapshot(rows)

    @staticmethod
    def _closure_snapshot(rows) -> str:
        fields = ("id", "feature_number", "category", "started_at_us", "ended_at_us", "notes",
                  "clickup_task_id", "clickup_time_entry_id", "sync_status", "sync_error", "revision")
        payload = [{field: row[field] for field in fields} for row in rows]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _covered_duration(spans: Sequence[tuple[int, int]]) -> int:
        if not spans:
            return 0
        total = 0
        current_start, current_end = sorted(spans)[0]
        for start, end in sorted(spans)[1:]:
            if start > current_end:
                total += current_end - current_start
                current_start, current_end = start, end
            else:
                current_end = max(current_end, end)
        return total + current_end - current_start

    def _day_bounds(self, day: dt.date) -> tuple[int, int]:
        if not isinstance(day, dt.date):
            raise ValidationError("Day must be a date.")
        try:
            next_day = day + dt.timedelta(days=1)
        except OverflowError as error:
            raise ValidationError("Day cannot be converted to a billing-day boundary.") from error
        start = dt.datetime.combine(day, dt.time.min, tzinfo=self.billing_timezone)
        end = dt.datetime.combine(next_day, dt.time.min, tzinfo=self.billing_timezone)
        return to_utc_microseconds(start), to_utc_microseconds(end)

    @classmethod
    def _append_event(
        cls, connection: sqlite3.Connection, interval_id: str, event_type: str, occurred_at_us: int,
        revision: int, before: Optional[WorkInterval], after: Optional[WorkInterval],
    ) -> None:
        payload = {"before": cls._event_snapshot(before), "after": cls._event_snapshot(after)}
        connection.execute(
            "INSERT INTO interval_events(id, interval_id, event_type, occurred_at_us, revision, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), interval_id, event_type, occurred_at_us, revision, json.dumps(payload, sort_keys=True)),
        )

    @staticmethod
    def _event_snapshot(interval: Optional[WorkInterval]) -> Optional[dict]:
        if interval is None:
            return None
        return {
            "id": interval.id,
            "feature_number": interval.feature_number,
            "category": interval.category,
            "started_at_us": to_utc_microseconds(interval.started_at),
            "ended_at_us": None if interval.ended_at is None else to_utc_microseconds(interval.ended_at),
            "notes": interval.notes,
            "clickup_task_id": interval.clickup_task_id,
            "clickup_time_entry_id": interval.clickup_time_entry_id,
            "sync_status": interval.sync_status,
            "sync_error": interval.sync_error,
            "session_id": interval.session_id,
            "correlation_id": interval.correlation_id,
            "deleted_at_us": None if interval.deleted_at is None else to_utc_microseconds(interval.deleted_at),
            "revision": interval.revision,
        }

    @staticmethod
    def _ensure_active_category(connection: sqlite3.Connection, category: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM categories WHERE name = ? AND retired_at_us IS NULL", (category,)
        ).fetchone()
        if row is None:
            raise ValidationError("Category does not exist or is retired.")

    def _interval_by_id(self, connection: sqlite3.Connection, interval_id: str, *, include_deleted: bool = False) -> WorkInterval:
        query = "SELECT * FROM intervals WHERE id = ?"
        if not include_deleted:
            query += " AND deleted_at_us IS NULL"
        row = connection.execute(query, (interval_id,)).fetchone()
        if row is None:
            raise NotFoundError("Interval does not exist.")
        return self._row_to_interval(row)

    @staticmethod
    def _row_to_interval(row: sqlite3.Row) -> WorkInterval:
        def instant(column: str) -> Optional[dt.datetime]:
            value = row[column]
            return None if value is None else from_utc_microseconds(value)
        return WorkInterval(
            id=row["id"], feature_number=row["feature_number"], category=row["category"],
            started_at=instant("started_at_us"), ended_at=instant("ended_at_us"), notes=row["notes"],
            clickup_task_id=row["clickup_task_id"], clickup_time_entry_id=row["clickup_time_entry_id"],
            sync_status=row["sync_status"], sync_error=row["sync_error"], session_id=row["session_id"],
            correlation_id=row["correlation_id"], created_at=instant("created_at_us"),
            updated_at=instant("updated_at_us"), deleted_at=instant("deleted_at_us"), revision=row["revision"],
        )

    @staticmethod
    def _raise_integrity_error(error: sqlite3.IntegrityError) -> None:
        message = str(error)
        if "one_active_interval_per_feature" in message:
            raise ActiveIntervalError("Feature already has an active interval.") from error
        if "interval overlaps" in message:
            raise IntervalOverlapError("Intervals for a feature cannot overlap.") from error
        raise error
