"""Synchronize permanent ClickUp task IDs, update frequency, and task status into records.json.

Policy:
- One persistent ClickUp task per feature.
- This script NEVER creates or modifies ClickUp tasks; ClickUp access is GET-only.
- A non-empty clickup_task_id is immutable during normal synchronization.
- Existing IDs are verified but never automatically replaced.
- Empty/missing IDs may be populated from exactly one matching feature-number tag.
- No matching task is persisted as an empty string.
- Ambiguous/conflicting identities are reported and left unresolved.
- Frequency is owned by the ClickUp custom field "Update frequency".
- A missing or invalid ClickUp frequency never erases the previously stored frequency.
- ClickUp task status is stored in clickup_status.
- A missing or unsupported ClickUp status never erases the previously stored clickup_status.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict

import psutil
from dotenv import load_dotenv

from .clickup_client import ClickUpClient, ClickUpError
from ...infrastructure.storage_paths import legacy_storage
from ...infrastructure.runtime_config import worker_runtime_settings

APP_ROOT = worker_runtime_settings(__file__).data_root
STORAGE = legacy_storage(APP_ROOT)
DATA_DIR = STORAGE.data
RECORDS_JSON = DATA_DIR / "records.json"
RECORDS_LOCK = DATA_DIR / "records.json.lock"
RECORDS_META_JSON = DATA_DIR / "records_meta.json"
LOGS_DIR = STORAGE.logs
LAST_RUN_JSON = STORAGE.path("clickup_last_run")
RECORDS_BACKUP_KEEP = 5

load_dotenv(APP_ROOT / ".env")

API_KEY = os.environ.get("API_KEY", "").strip()
CLICKUP_LIST_ID = os.environ.get("CLICKUP_LIST_ID", "").strip()
UPDATE_FREQUENCY_FIELD_NAME = "Update frequency"
CLICKUP_STATUS_OPTIONS = {
    "complete": "Complete",
    "in progress": "In Progress",
    "to do": "To do",
    "to be checked": "To be checked",
}
def _records_backup_path(path: Path, generation: int) -> Path:
    return path.with_name(f"{path.name}.bak.{generation}")


def _rotate_record_backups(path: Path, keep: int = RECORDS_BACKUP_KEEP) -> None:
    if keep <= 0 or not path.exists():
        return

    for generation in range(keep, 1, -1):
        src = _records_backup_path(path, generation - 1)
        dst = _records_backup_path(path, generation)
        if src.exists():
            os.replace(src, dst)

    newest = _records_backup_path(path, 1)
    handle, temporary = tempfile.mkstemp(
        prefix=newest.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(path.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, newest)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def acquire_lock(lock_path: Path, timeout_sec: float = 60.0, poll_sec: float = 0.2) -> bool:
    """Cross-process lock compatible with the Automator records.json lock."""
    deadline = time.time() + max(0.0, float(timeout_sec))

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(f"pid={os.getpid()}\n")
                stream.write(f"ts={datetime.now().isoformat(timespec='seconds')}\n")
            return True
        except FileExistsError:
            stale = False
            try:
                text = lock_path.read_text(encoding="utf-8", errors="replace")
                match = re.search(r"(?m)^pid=(\d+)\s*$", text)
                if match:
                    if not psutil.pid_exists(int(match.group(1))):
                        stale = True
                else:
                    if time.time() - lock_path.stat().st_mtime >= 600:
                        stale = True
            except FileNotFoundError:
                continue
            except Exception:
                stale = False

            if stale:
                try:
                    lock_path.unlink()
                    continue
                except FileNotFoundError:
                    continue
                except Exception:
                    pass

            if time.time() >= deadline:
                return False
            time.sleep(poll_sec)
        except Exception:
            return False


def release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def load_records() -> list[dict]:
    if not RECORDS_JSON.is_file():
        raise FileNotFoundError(f"Missing records.json: {RECORDS_JSON}")

    value = json.loads(RECORDS_JSON.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("records.json root must be a JSON list.")

    records: list[dict] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"records.json item {index} is not an object.")
        records.append(item)
    return records


def bump_records_version(writer: str = "sync_clickup_task_ids") -> None:
    meta = {}
    try:
        if RECORDS_META_JSON.is_file():
            value = json.loads(RECORDS_META_JSON.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                meta = value
    except Exception:
        meta = {}

    try:
        version = int(meta.get("records_version", 0))
    except (TypeError, ValueError):
        version = 0

    meta["records_version"] = version + 1
    meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
    meta["writer"] = writer

    handle, temporary = tempfile.mkstemp(
        prefix=RECORDS_META_JSON.name + ".",
        suffix=".tmp",
        dir=str(RECORDS_META_JSON.parent),
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(meta, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, RECORDS_META_JSON)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def atomic_write_records(records: list[dict]) -> None:
    _rotate_record_backups(RECORDS_JSON)

    handle, temporary = tempfile.mkstemp(
        prefix=RECORDS_JSON.name + ".",
        suffix=".tmp",
        dir=str(RECORDS_JSON.parent),
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(records, stream, indent=4, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, RECORDS_JSON)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass

    bump_records_version()


def _task_tags(task: dict) -> set[str]:
    result = set()
    for raw_tag in task.get("tags", []) or []:
        if not isinstance(raw_tag, dict):
            continue
        name = str(raw_tag.get("name", "")).strip()
        if name:
            result.add(name)
    return result

def _canonical_frequency(value) -> str:
    text = (
        ""
        if value is None
        else str(value).strip()
    )

    aliases = {
        "daily": "Daily",
        "weekly": "Weekly",
        "monthly": "Monthly",
        "quarterly": "Quarterly",
        "biannually": "Biannually",
        "biannual": "Biannually",
        "every 6 months": "Biannually",
        "every six months": "Biannually",
        "annually": "Annually",
        "annual": "Annually",
        "yearly": "Annually",
    }

    result = aliases.get(
        text.casefold()
    )

    if result is None:
        raise ClickUpError(
            "Unsupported ClickUp Update frequency "
            "value: %r"
            % text
        )

    return result


def _task_update_frequency(
    task: dict,
) -> str:
    fields = (
        task.get(
            "custom_fields",
            [],
        )
        or []
    )

    matches = [
        field
        for field in fields
        if (
            isinstance(
                field,
                dict,
            )
            and str(
                field.get(
                    "name",
                    "",
                )
            ).strip().casefold()
            == UPDATE_FREQUENCY_FIELD_NAME.casefold()
        )
    ]

    if len(matches) != 1:
        raise ClickUpError(
            "Expected exactly one ClickUp custom field "
            "named 'Update frequency'."
        )

    field = matches[0]
    raw_value = field.get(
        "value"
    )

    if raw_value is None:
        raise ClickUpError(
            "ClickUp Update frequency is blank."
        )

    raw_tokens: set[str] = set()

    def add_token(value) -> None:
        if value is None:
            return

        text = str(
            value
        ).strip()

        if text:
            raw_tokens.add(
                text.casefold()
            )

    if isinstance(
        raw_value,
        dict,
    ):
        for key in (
            "id",
            "name",
            "value",
            "orderindex",
        ):
            add_token(
                raw_value.get(
                    key
                )
            )

    elif isinstance(
        raw_value,
        (
            list,
            tuple,
            set,
        ),
    ):
        for item in raw_value:
            if isinstance(
                item,
                dict,
            ):
                for key in (
                    "id",
                    "name",
                    "value",
                    "orderindex",
                ):
                    add_token(
                        item.get(
                            key
                        )
                    )
            else:
                add_token(
                    item
                )

    else:
        add_token(
            raw_value
        )

    options = (
        field.get(
            "type_config"
        )
        or {}
    ).get(
        "options",
        [],
    )

    for option in options:
        if not isinstance(
            option,
            dict,
        ):
            continue

        option_tokens = set()

        for key in (
            "id",
            "name",
            "value",
            "orderindex",
        ):
            value = option.get(
                key
            )

            if value is not None:
                text = str(
                    value
                ).strip()

                if text:
                    option_tokens.add(
                        text.casefold()
                    )

        if (
            raw_tokens
            & option_tokens
        ):
            return _canonical_frequency(
                option.get(
                    "name",
                    "",
                )
            )

    for token in sorted(
        raw_tokens
    ):
        try:
            return _canonical_frequency(
                token
            )

        except ClickUpError:
            continue

    raise ClickUpError(
        "Could not resolve ClickUp "
        "'Update frequency' value: %r"
        % raw_value
    )


def _task_clickup_status(task: dict) -> str:
    raw_status = ClickUpClient.task_status_name(task)
    canonical_status = CLICKUP_STATUS_OPTIONS.get(
        raw_status.casefold()
    )

    if canonical_status is None:
        if not raw_status:
            raise ClickUpError(
                "ClickUp task status is blank."
            )

        raise ClickUpError(
            "Unsupported ClickUp task status: %r"
            % raw_status
        )

    return canonical_status

def _task_dataops_status(task: dict) -> str:
    """Resolve the unique, non-blank WorkflowStatus value without guessing."""
    fields = task.get("custom_fields", []) or []

    matches = [
        field
        for field in fields
        if isinstance(field, dict)
        and str(field.get("name", "")).strip().casefold()
        == "workflowstatus"
    ]

    if len(matches) != 1:
        raise ClickUpError(
            "Expected exactly one ClickUp field named 'WorkflowStatus'."
        )

    field = matches[0]
    value = field.get("value")

    if value is None or value == "":
        raise ClickUpError("ClickUp WorkflowStatus is blank.")

    field_type = str(field.get("type", "")).strip()

    if field_type in {"short_text", "text"}:
        if not isinstance(value, str) or not value.strip():
            raise ClickUpError("Invalid text WorkflowStatus value.")
        return value.strip()

    if field_type != "drop_down":
        raise ClickUpError(
            "Unsupported WorkflowStatus field type: %r" % field_type
        )

    options = (field.get("type_config") or {}).get("options", [])

    if not isinstance(options, list):
        raise ClickUpError("WorkflowStatus dropdown options are invalid.")

    if isinstance(value, dict):
        tokens = {
            str(value[key]).strip()
            for key in ("id", "name", "value", "orderindex")
            if value.get(key) is not None
        }
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        tokens = {str(value).strip()}
    else:
        raise ClickUpError("Unsupported WorkflowStatus dropdown value.")

    if not tokens or "" in tokens:
        raise ClickUpError("Invalid WorkflowStatus dropdown selection.")

    selected = [
        option
        for option in options
        if isinstance(option, dict)
        and any(
            str(option.get(key, "")).strip() in tokens
            for key in ("id", "name", "orderindex")
        )
    ]

    if len(selected) != 1 or not str(
        selected[0].get("name", "")
    ).strip():
        raise ClickUpError(
            "WorkflowStatus dropdown selection is ambiguous or unknown."
        )

    return str(selected[0]["name"]).strip()


def _apply_dataops_status(
    record,
    task,
    feature,
    task_id,
    verified,
    changed_features,
    issues,
):
    try:
        value = _task_dataops_status(task)
    except ClickUpError as exc:
        issues.append({
            "feature_number": feature,
            "task_id": task_id,
            "detail": str(exc),
        })
        return False

    verified.add(feature)

    if str(record.get("status", "") or "").strip() == value:
        return False

    record["status"] = value
    changed_features.add(feature)
    return True

ABSENT_TASK_NOTE = "Ignore. This task has been removed from the list"


def _update_absence_note(record: dict, *, absent: bool) -> bool:
    """Manage only the exact absence prefix in the local notes field."""
    original = str(record.get("notes", "") or "")
    prefix = ABSENT_TASK_NOTE + "\n"

    if absent:
        if original == ABSENT_TASK_NOTE or original.startswith(prefix):
            return False

        record["notes"] = (
            prefix + original if original else ABSENT_TASK_NOTE
        )
        return True

    if original == ABSENT_TASK_NOTE:
        record["notes"] = ""
        return True

    if original.startswith(prefix):
        record["notes"] = original[len(prefix):]
        return True

    return False

def scan_clickup_list(client: ClickUpClient) -> list[dict]:
    """Read all tasks from the configured list. No ClickUp mutation occurs."""
    tasks: list[dict] = []
    page = 0

    while True:
        result = client.request(
            "GET",
            f"/list/{client.list_id}/task",
            params={
                "page": page,
                "include_closed": "true",
                "subtasks": "true",
            },
        )
        if not isinstance(result, dict):
            raise ClickUpError("Invalid ClickUp list response.")

        page_tasks = result.get("tasks")
        if not isinstance(page_tasks, list):
            raise ClickUpError(
                "ClickUp list response did not contain a task list."
            )

        for task in page_tasks:
            if (
                    not isinstance(task, dict)
                    or not str(task.get("id", "")).strip()
                    or not isinstance(task.get("tags"), list)
                    or any(
                not isinstance(tag, dict)
                for tag in task["tags"]
            )
            ):
                raise ClickUpError(
                    "ClickUp returned an invalid task or tag record; "
                    "absence cannot be determined safely."
                )

        tasks.extend(page_tasks)

        last_page = result.get("last_page")
        if last_page is True:
            break

        if last_page is not False:
            raise ClickUpError(
                "ClickUp did not explicitly confirm pagination "
                "completion; absence cannot be determined safely."
            )

        if not page_tasks:
            raise ClickUpError(
                "ClickUp returned an empty non-final page."
            )

        page += 1
    return tasks


def _write_last_run(payload: dict) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        LAST_RUN_JSON.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def main() -> int:
    started_at = datetime.now().isoformat(timespec="seconds")

    if not API_KEY:
        raise RuntimeError("API_KEY is missing from the application environment.")
    if not CLICKUP_LIST_ID:
        raise RuntimeError("CLICKUP_LIST_ID is missing from the application environment.")

    records = load_records()

    feature_rows: Dict[str, dict] = {}
    original_ids: Dict[str, str] = {}
    original_frequencies: Dict[str, str] = {}
    original_clickup_statuses: Dict[str, str] = {}
    invalid_record_features: list[str] = []

    for index, record in enumerate(records):
        feature = str(
            record.get(
                "feature_number",
                "",
            )
        ).strip()

        if (
                not feature
                or not feature.isascii()
                or not feature.isdigit()
        ):
            invalid_record_features.append(
                feature
                or f"<row {index}>"
            )
            continue

        if feature in feature_rows:
            raise RuntimeError(
                f"records.json contains duplicate "
                f"feature_number {feature}."
            )

        feature_rows[feature] = record

        original_ids[feature] = str(
            record.get(
                "clickup_task_id",
                "",
            )
            or ""
        ).strip()

        original_frequencies[feature] = str(
            record.get(
                "frequency",
                "",
            )
            or ""
        ).strip()
        original_clickup_statuses[feature] = str(
            record.get(
                "clickup_status",
                "",
            )
            or ""
        ).strip()
    original_dataops_statuses = {
        feature: str(row.get("status", "") or "").strip()
        for feature, row in feature_rows.items()
    }
    original_notes_by_feature = {
        feature: str(row.get("notes", "") or "")
        for feature, row in feature_rows.items()
    }
    client = ClickUpClient(API_KEY, CLICKUP_LIST_ID)
    print(
        f"Scanning ClickUp list {CLICKUP_LIST_ID} "
        f"for {len(feature_rows)} feature records..."
    )
    tasks = scan_clickup_list(client)

    tasks_by_id: Dict[str, dict] = {}
    tasks_by_feature: Dict[str, list[dict]] = defaultdict(list)
    known_features = set(feature_rows)

    for task in tasks:
        task_id = str(task.get("id", "")).strip()
        if not task_id:
            continue
        tasks_by_id[task_id] = task
        for tag in _task_tags(task):
            if tag in known_features:
                tasks_by_feature[tag].append(task)

    stored_id_to_features: Dict[str, list[str]] = defaultdict(list)
    for feature, stored_id in original_ids.items():
        if stored_id:
            stored_id_to_features[stored_id].append(feature)

    duplicate_stored_ids = {
        task_id: tuple(sorted(features))
        for task_id, features in stored_id_to_features.items()
        if len(features) > 1
    }

    task_to_recognized_features: Dict[str, set[str]] = defaultdict(set)
    for feature, matching_tasks in tasks_by_feature.items():
        for task in matching_tasks:
            task_id = str(task.get("id", "")).strip()
            if task_id:
                task_to_recognized_features[task_id].add(feature)

    multi_feature_tasks = {
        task_id: tuple(sorted(features))
        for task_id, features in task_to_recognized_features.items()
        if len(features) > 1
    }

    outcomes: list[dict] = []
    changed = False
    discovered_ids: Dict[str, str] = {}

    frequency_verified: set[str] = set()
    frequency_changed: set[str] = set()
    frequency_issues: list[dict] = []
    clickup_status_verified: set[str] = set()
    clickup_status_changed: set[str] = set()
    clickup_status_issues: list[dict] = []
    dataops_status_verified: set[str] = set()
    dataops_status_changed: set[str] = set()
    dataops_status_issues: list[dict] = []
    notes_marked_absent: set[str] = set()
    notes_restored: set[str] = set()
    notes_changed: set[str] = set()

    for feature in sorted(feature_rows, key=int):
        record = feature_rows[feature]
        stored_id = original_ids[feature]
        matching_tasks = tasks_by_feature.get(feature, [])
        unique_matching = {
            str(task.get("id", "")).strip(): task
            for task in matching_tasks
            if str(task.get("id", "")).strip()
        }

        # Established identity: verify only; never replace automatically.
        if stored_id:
            if stored_id in duplicate_stored_ids:
                outcomes.append({
                    "feature_number": feature,
                    "result": "IDENTITY CONFLICT",
                    "task_id": stored_id,
                    "detail": (
                        "The same stored ClickUp task ID is assigned to multiple features: "
                        + ", ".join(duplicate_stored_ids[stored_id])
                    ),
                })
                continue

            task = tasks_by_id.get(stored_id)
            if task is None:
                candidate_ids = sorted(unique_matching)
                detail = (
                    "Stored task ID was not found in the configured ClickUp list. "
                    "The ID was not changed."
                )
                if candidate_ids:
                    detail += (
                        " Other task(s) carry this feature tag: "
                        + ", ".join(candidate_ids)
                        + ". Manual identity review is required."
                    )

                if not candidate_ids:
                    if _update_absence_note(record, absent=True):
                        notes_marked_absent.add(feature)
                        notes_changed.add(feature)
                        changed = True

                outcomes.append({
                    "feature_number": feature,
                    "result": "IDENTITY CONFLICT",
                    "task_id": stored_id,
                    "detail": detail,
                })
                continue

            if feature not in _task_tags(task):
                candidate_ids = sorted(unique_matching)
                detail = (
                    "Stored task no longer has the exact feature-number tag. "
                    "The ID was not changed."
                )
                if candidate_ids:
                    detail += (
                        " Other task(s) carry this feature tag: "
                        + ", ".join(candidate_ids)
                        + ". Manual identity review is required."
                    )
                outcomes.append({
                    "feature_number": feature,
                    "result": "IDENTITY CONFLICT",
                    "task_id": stored_id,
                    "detail": detail,
                })
                continue

            if stored_id in multi_feature_tasks:
                outcomes.append({
                    "feature_number": feature,
                    "result": "IDENTITY CONFLICT",
                    "task_id": stored_id,
                    "detail": (
                        "This ClickUp task carries feature-number tags for multiple records: "
                        + ", ".join(multi_feature_tasks[stored_id])
                    ),
                })
                continue

            if len(unique_matching) > 1:
                outcomes.append({
                    "feature_number": feature,
                    "result": "AMBIGUOUS",
                    "task_id": stored_id,
                    "detail": (
                        "Multiple ClickUp tasks carry this feature tag. The established ID "
                        "was preserved and no automatic reassignment was attempted."
                    ),
                })
                continue

            try:
                new_frequency = (
                    _task_update_frequency(
                        task
                    )
                )

            except ClickUpError as exc:
                frequency_issues.append({
                    "feature_number": feature,
                    "task_id": stored_id,
                    "detail": str(exc),
                })

            else:
                frequency_verified.add(
                    feature
                )

                current_frequency = str(
                    record.get(
                        "frequency",
                        "",
                    )
                    or ""
                ).strip()

                if (
                        current_frequency
                        != new_frequency
                ):
                    record[
                        "frequency"
                    ] = new_frequency

                    frequency_changed.add(
                        feature
                    )

                    changed = True
            try:
                new_clickup_status = (
                    _task_clickup_status(
                        task
                    )
                )
            except ClickUpError as exc:
                clickup_status_issues.append({
                    "feature_number": feature,
                    "task_id": stored_id,
                    "detail": str(exc),
                })
            else:
                clickup_status_verified.add(
                    feature
                )

                current_clickup_status = str(
                    record.get(
                        "clickup_status",
                        "",
                    )
                    or ""
                ).strip()

                if current_clickup_status != new_clickup_status:
                    record[
                        "clickup_status"
                    ] = new_clickup_status

                    clickup_status_changed.add(
                        feature
                    )

                    changed = True

            changed |= _apply_dataops_status(
                record,
                task,
                feature,
                stored_id,
                dataops_status_verified,
                dataops_status_changed,
                dataops_status_issues,
            )

            if _update_absence_note(record, absent=False):
                notes_restored.add(feature)
                notes_changed.add(feature)
                changed = True

            outcomes.append({
                "feature_number": feature,
                "result": "VERIFIED",
                "task_id": stored_id,
                "detail": "",
            })
            continue

        # Empty/missing identity: discovery is permitted.
        if len(unique_matching) == 0:
            if "clickup_task_id" not in record:
                record["clickup_task_id"] = ""
                changed = True

            if _update_absence_note(record, absent=True):
                notes_marked_absent.add(feature)
                notes_changed.add(feature)
                changed = True

            outcomes.append({
                "feature_number": feature,
                "result": "NO TASK",
                "task_id": "",
                "detail": "No task carries the exact feature-number tag in the configured list.",
            })
            continue

        if len(unique_matching) > 1:
            if "clickup_task_id" not in record:
                record["clickup_task_id"] = ""
                changed = True
            outcomes.append({
                "feature_number": feature,
                "result": "AMBIGUOUS",
                "task_id": "",
                "detail": f"{len(unique_matching)} ClickUp tasks carry this feature tag.",
            })
            continue

        task_id, _task = next(iter(unique_matching.items()))

        if task_id in multi_feature_tasks:
            if "clickup_task_id" not in record:
                record["clickup_task_id"] = ""
                changed = True
            outcomes.append({
                "feature_number": feature,
                "result": "IDENTITY CONFLICT",
                "task_id": "",
                "detail": (
                    "The matching ClickUp task carries feature-number tags for multiple records: "
                    + ", ".join(multi_feature_tasks[task_id])
                ),
            })
            continue

        existing_owners = stored_id_to_features.get(task_id, [])
        if existing_owners:
            if "clickup_task_id" not in record:
                record["clickup_task_id"] = ""
                changed = True
            outcomes.append({
                "feature_number": feature,
                "result": "IDENTITY CONFLICT",
                "task_id": "",
                "detail": (
                    "The matching task ID is already permanently assigned to feature(s): "
                    + ", ".join(sorted(existing_owners))
                ),
            })
            continue

        discovered_owner = discovered_ids.get(task_id)
        if discovered_owner and discovered_owner != feature:
            if "clickup_task_id" not in record:
                record["clickup_task_id"] = ""
                changed = True
            outcomes.append({
                "feature_number": feature,
                "result": "IDENTITY CONFLICT",
                "task_id": "",
                "detail": (
                    "The same task was also discovered for feature "
                    f"{discovered_owner}. No assignment was made."
                ),
            })
            continue

        record["clickup_task_id"] = task_id
        discovered_ids[task_id] = feature
        changed = True

        try:
            new_frequency = (
                _task_update_frequency(
                    _task
                )
            )

        except ClickUpError as exc:
            frequency_issues.append({
                "feature_number": feature,
                "task_id": task_id,
                "detail": str(exc),
            })

        else:
            frequency_verified.add(
                feature
            )

            current_frequency = str(
                record.get(
                    "frequency",
                    "",
                )
                or ""
            ).strip()

            if (
                    current_frequency
                    != new_frequency
            ):
                record[
                    "frequency"
                ] = new_frequency

                frequency_changed.add(
                    feature
                )

                changed = True
        try:
            new_clickup_status = (
                _task_clickup_status(
                    _task
                )
            )
        except ClickUpError as exc:
            clickup_status_issues.append({
                "feature_number": feature,
                "task_id": task_id,
                "detail": str(exc),
            })
        else:
            clickup_status_verified.add(
                feature
            )

            current_clickup_status = str(
                record.get(
                    "clickup_status",
                    "",
                )
                or ""
            ).strip()

            if (
                    current_clickup_status
                    != new_clickup_status
            ):
                record[
                    "clickup_status"
                ] = new_clickup_status

                clickup_status_changed.add(
                    feature
                )

                changed = True

        changed |= _apply_dataops_status(
            record,
            _task,
            feature,
            task_id,
            dataops_status_verified,
            dataops_status_changed,
            dataops_status_issues,
        )

        if _update_absence_note(record, absent=False):
            notes_restored.add(feature)
            notes_changed.add(feature)
            changed = True

        outcomes.append({
            "feature_number": feature,
            "result": "FOUND",
            "task_id": task_id,
            "detail": "",
        })

    for value in invalid_record_features:
        outcomes.append({
            "feature_number": value,
            "result": "INVALID RECORD",
            "task_id": "",
            "detail": "Feature number is blank or not ASCII digits.",
        })

    if changed:
        if not acquire_lock(RECORDS_LOCK, timeout_sec=60.0):
            raise RuntimeError("records.json remained locked too long; no changes were written.")

        try:
            current = load_records()
            current_by_feature = {
                str(item.get("feature_number", "")).strip(): item
                for item in current
                if isinstance(item, dict)
            }

            if set(current_by_feature) != set(feature_rows):
                raise RuntimeError(
                    "records.json changed while ClickUp was being scanned. "
                    "No changes were written; run the sync again."
                )

            for feature, intended_record in feature_rows.items():
                current_record = current_by_feature[feature]
                current_id = str(
                    current_record.get(
                        "clickup_task_id",
                        "",
                    )
                    or ""
                ).strip()

                original_id = original_ids[feature]

                intended_id = str(
                    intended_record.get(
                        "clickup_task_id",
                        "",
                    )
                    or ""
                ).strip()

                # If another process established a different permanent ID while
                # this scan was running, abort rather than overwriting it.
                if current_id != original_id:
                    if (
                            current_id
                            and current_id != intended_id
                    ):
                        raise RuntimeError(
                            f"Feature {feature} acquired a different non-empty "
                            "ClickUp task ID while the sync was running. "
                            "No changes were written."
                        )

                    if (
                            original_id
                            and current_id != original_id
                    ):
                        raise RuntimeError(
                            f"Feature {feature}'s established ClickUp task ID "
                            "changed while the sync was running. "
                            "No changes were written."
                        )

                current_record[
                    "clickup_task_id"
                ] = intended_id

                if feature in frequency_verified:
                    current_frequency = str(
                        current_record.get(
                            "frequency",
                            "",
                        )
                        or ""
                    ).strip()

                    original_frequency = (
                        original_frequencies[
                            feature
                        ]
                    )

                    intended_frequency = str(
                        intended_record.get(
                            "frequency",
                            "",
                        )
                        or ""
                    ).strip()

                    if (
                            current_frequency
                            != original_frequency
                            and current_frequency
                            != intended_frequency
                    ):
                        raise RuntimeError(
                            f"Feature {feature}'s frequency changed "
                            "while the ClickUp sync was running. "
                            "No changes were written."
                        )

                    current_record[
                        "frequency"
                    ] = intended_frequency

                if feature in clickup_status_verified:
                    current_clickup_status = str(
                        current_record.get(
                            "clickup_status",
                            "",
                        )
                        or ""
                    ).strip()

                    original_clickup_status = (
                        original_clickup_statuses[
                            feature
                        ]
                    )

                    intended_clickup_status = str(
                        intended_record.get(
                            "clickup_status",
                            "",
                        )
                        or ""
                    ).strip()

                    if (
                            current_clickup_status
                            != original_clickup_status
                            and current_clickup_status
                            != intended_clickup_status
                    ):
                        raise RuntimeError(
                            f"Feature {feature}'s ClickUp status changed "
                            "while the ClickUp sync was running. "
                            "No changes were written."
                        )

                    current_record[
                        "clickup_status"
                    ] = intended_clickup_status

                if feature in dataops_status_verified:
                    current_status = str(
                        current_record.get("status", "") or ""
                    ).strip()

                    original_status = original_dataops_statuses[feature]

                    intended_status = str(
                        intended_record.get("status", "") or ""
                    ).strip()

                    if current_status not in {
                        original_status,
                        intended_status,
                    }:
                        raise RuntimeError(
                            f"Feature {feature}'s local status changed "
                            "during ClickUp sync; no changes were written."
                        )

                    current_record["status"] = intended_status
                if feature in notes_changed:
                    current_notes = str(
                        current_record.get("notes", "") or ""
                    )
                    original_notes = original_notes_by_feature[feature]
                    intended_notes = str(
                        intended_record.get("notes", "") or ""
                    )

                    if current_notes not in {
                        original_notes,
                        intended_notes,
                    }:
                        raise RuntimeError(
                            f"Feature {feature}'s notes changed "
                            "during ClickUp sync; no changes were written."
                        )

                    current_record["notes"] = intended_notes
            atomic_write_records(current)
        finally:
            release_lock(RECORDS_LOCK)

    counts: Dict[str, int] = defaultdict(int)
    for outcome in outcomes:
        counts[outcome["result"]] += 1

    print()
    print("CLICKUP TASK + FREQUENCY + STATUS SYNC")
    print(f"Records checked:          {len(feature_rows)}")
    print(f"ClickUp tasks scanned:    {len(tasks)}")
    print(f"Verified existing IDs:    {counts['VERIFIED']}")
    print(f"New IDs discovered:       {counts['FOUND']}")
    print(f"No ClickUp task:           {counts['NO TASK']}")
    print(f"Ambiguous tags:            {counts['AMBIGUOUS']}")
    print(f"Identity conflicts:        {counts['IDENTITY CONFLICT']}")
    print(f"Invalid records:           {counts['INVALID RECORD']}")
    print(f"Frequencies verified:      {len(frequency_verified)}")
    print(f"Frequencies changed:       {len(frequency_changed)}")
    print(f"Frequency issues:          {len(frequency_issues)}")
    print(f"ClickUp statuses verified: {len(clickup_status_verified)}")
    print(f"ClickUp statuses changed:  {len(clickup_status_changed)}")
    print(f"ClickUp status issues:     {len(clickup_status_issues)}")
    print(f"WorkflowStatus verified:    {len(dataops_status_verified)}")
    print(f"WorkflowStatus changed:     {len(dataops_status_changed)}")
    print(f"WorkflowStatus issues:      {len(dataops_status_issues)}")
    print(f"Notes marked absent:       {len(notes_marked_absent)}")
    print(f"Notes restored:            {len(notes_restored)}")
    print("records.json updated:      " + ("YES" if changed else "NO"))
    print()
    print("No ClickUp tasks were created or modified.")

    interesting = {
        "FOUND",
        "AMBIGUOUS",
        "IDENTITY CONFLICT",
        "INVALID RECORD",
    }
    details = [row for row in outcomes if row["result"] in interesting]
    if details:
        print()
        print("DETAILS")
        for row in details:
            task_suffix = f" -> {row['task_id']}" if row["task_id"] else ""
            detail_suffix = f" | {row['detail']}" if row["detail"] else ""
            print(f"{row['feature_number']}: {row['result']}{task_suffix}{detail_suffix}")
    if frequency_issues:
        print()
        print("FREQUENCY ISSUES")

        for row in frequency_issues:
            print(
                f"{row['feature_number']}: "
                f"{row['detail']}"
            )

    if clickup_status_issues:
        print()
        print("CLICKUP STATUS ISSUES")

        for row in clickup_status_issues:
            print(
                f"{row['feature_number']}: "
                f"{row['detail']}"
            )

    _write_last_run({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "started_at": started_at,
        "ok": True,
        "records_checked": len(feature_rows),
        "clickup_tasks_scanned": len(tasks),
        "records_json_updated": changed,
        "counts": dict(sorted(counts.items())),
        "frequency_verified": sorted(frequency_verified),
        "frequency_changed": sorted(frequency_changed),
        "frequency_issues": frequency_issues,
        "clickup_status_verified": sorted(clickup_status_verified),
        "clickup_status_changed": sorted(clickup_status_changed),
        "clickup_status_issues": clickup_status_issues,
        "dataops_status_verified": sorted(dataops_status_verified),
        "dataops_status_changed": sorted(dataops_status_changed),
        "dataops_status_issues": dataops_status_issues,
        "notes_marked_absent": sorted(notes_marked_absent),
        "notes_restored": sorted(notes_restored),
        "outcomes": outcomes,
    })

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _write_last_run({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "ok": False,
            "error": str(exc),
        })
        print(f"CLICKUP TASK ID SYNC FAILED: {exc}")
        raise
