"""Small ClickUp API v2 client for known Feature Manager work items."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote

import requests
import json
import os
import tempfile
import re
import time
import threading

BASE_URL = "https://api.clickup.com/api/v2"
CLICKUP_MIN_REQUEST_INTERVAL_SECONDS = 1.0
CLICKUP_RATE_LIMIT_RETRIES = 6
CLICKUP_RATE_LIMIT_FALLBACK_SECONDS = 5.0
CLICKUP_RATE_LIMIT_MAX_WAIT_SECONDS = 60.0

def load_task_id_cache(path: Path | str) -> Dict[str, str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for feature, value in data.items():
        feature = str(feature)
        if not feature.isascii() or not feature.isdigit():
            continue
        task_id = value.get("task_sync", {}).get("clickup_task_id") if isinstance(value, dict) else value
        if task_id:
            result[feature] = str(task_id)
    return result


def save_task_id_cache(path: Path | str, mapping: Dict[str, str]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(destination.read_text(encoding="utf-8")) if destination.is_file() else {}
    except (OSError, ValueError, TypeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    nested_state = destination.name == "clickup_sync_state.json" or any(isinstance(value, dict) and any(
        key in value for key in ("task_sync", "attachment_sync", "time_sync")
    ) for value in existing.values())
    if nested_state:
        for feature, task_id in mapping.items():
            row = existing.setdefault(str(feature), {})
            if not isinstance(row, dict):
                row = existing[str(feature)] = {}
            domain = row.setdefault("task_sync", {})
            if not isinstance(domain, dict):
                domain = row["task_sync"] = {}
            domain["clickup_task_id"] = str(task_id)
        value_to_save = existing
    else:
        value_to_save = mapping
    handle, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value_to_save, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


class ClickUpError(RuntimeError):
    """ClickUp request or response validation failed."""


class ClickUpTaskNotFound(ClickUpError):
    """No existing task has the requested exact feature-number tag."""


class AmbiguousClickUpTask(ClickUpError):
    """More than one existing task has the requested feature-number tag."""


class ClickUpClient:
    def __init__(
            self,
            token: str,
            list_id: str,
            *,
            session=None,
            base_url: str = BASE_URL,
            timeout: int = 30,
    ):
        token = str(
            token or ""
        ).strip()

        list_id = str(
            list_id or ""
        ).strip()

        if not token:
            raise ValueError(
                "ClickUp API token is required."
            )

        if not list_id:
            raise ValueError(
                "ClickUp list ID is required."
            )

        self.list_id = list_id
        self.base_url = (
            base_url.rstrip("/")
        )
        self.timeout = timeout
        self.session = (
                session
                or requests.Session()
        )

        self.session.headers.update(
            {
                "Authorization": token
            }
        )

        self._request_lock = (
            threading.Lock()
        )

        self._last_request_monotonic = (
            0.0
        )

    @staticmethod
    def _rate_limit_wait_seconds(response, retry_number: int) -> float:
        headers = getattr(response, "headers", {}) or {}

        retry_after = headers.get("Retry-After")
        if retry_after is not None:
            try:
                return min(
                    CLICKUP_RATE_LIMIT_MAX_WAIT_SECONDS,
                    max(
                        CLICKUP_MIN_REQUEST_INTERVAL_SECONDS,
                        float(retry_after),
                    ),
                )
            except (TypeError, ValueError):
                pass

        reset = headers.get("X-RateLimit-Reset")
        if reset is not None:
            try:
                reset_at = float(reset)

                # Accept either epoch seconds or epoch milliseconds.
                if reset_at > 1_000_000_000_000:
                    reset_at /= 1000.0

                seconds = (
                        reset_at
                        - time.time()
                        + 0.5
                )

                if seconds > 0:
                    return min(
                        CLICKUP_RATE_LIMIT_MAX_WAIT_SECONDS,
                        max(
                            CLICKUP_MIN_REQUEST_INTERVAL_SECONDS,
                            seconds,
                        ),
                    )

            except (TypeError, ValueError):
                pass

        return min(
            CLICKUP_RATE_LIMIT_MAX_WAIT_SECONDS,
            CLICKUP_RATE_LIMIT_FALLBACK_SECONDS
            * (
                    2
                    ** max(
                0,
                int(retry_number),
            )
            ),
        )

    def request(
            self,
            method: str,
            path: str,
            **kwargs,
    ) -> Dict[str, Any]:
        url = self.base_url + path

        if "timeout" not in kwargs:
            kwargs["timeout"] = self.timeout

        # Multipart uploads use an open file handle. If ClickUp rejects the
        # request with 429, rewind the file before rebuilding the retry.
        rewindable_files = []

        files = kwargs.get(
            "files"
        )

        if isinstance(
                files,
                dict,
        ):
            for value in files.values():
                stream = (
                    value[1]
                    if (
                            isinstance(
                                value,
                                (
                                    tuple,
                                    list,
                                ),
                            )
                            and len(value) > 1
                    )
                    else value
                )

                if (
                        hasattr(
                            stream,
                            "tell",
                        )
                        and hasattr(
                    stream,
                    "seek",
                )
                ):
                    try:
                        rewindable_files.append(
                            (
                                stream,
                                stream.tell(),
                            )
                        )
                    except (
                            OSError,
                            ValueError,
                    ):
                        pass

        # Serialize ClickUp traffic from this client. This prevents GUI or
        # workflow threads from producing a burst even if they overlap.
        with self._request_lock:
            response = None

            for retry_number in range(
                    CLICKUP_RATE_LIMIT_RETRIES
                    + 1
            ):
                if retry_number:
                    for (
                            stream,
                            position,
                    ) in rewindable_files:
                        try:
                            stream.seek(
                                position
                            )
                        except (
                                OSError,
                                ValueError,
                        ):
                            raise ClickUpError(
                                "Could not rewind a ClickUp upload "
                                "for rate-limit retry."
                            )

                elapsed = (
                        time.monotonic()
                        - self._last_request_monotonic
                )

                remaining = (
                        CLICKUP_MIN_REQUEST_INTERVAL_SECONDS
                        - elapsed
                )

                if remaining > 0:
                    time.sleep(
                        remaining
                    )

                response = self.session.request(
                    method,
                    url,
                    **kwargs,
                )

                self._last_request_monotonic = (
                    time.monotonic()
                )

                if response.status_code != 429:
                    break

                if (
                        retry_number
                        >= CLICKUP_RATE_LIMIT_RETRIES
                ):
                    break

                time.sleep(
                    self._rate_limit_wait_seconds(
                        response,
                        retry_number,
                    )
                )

        if response is None:
            raise ClickUpError(
                "ClickUp request did not produce a response."
            )

        if not response.ok:
            if response.status_code == 404:
                raise ClickUpError(
                    "ClickUp resource was not found: %s"
                    % path
                )

            raise ClickUpError(
                "ClickUp %s %s failed (%s): %s"
                % (
                    method.upper(),
                    path,
                    response.status_code,
                    str(
                        getattr(
                            response,
                            "text",
                            "",
                        )
                    )[:1000],
                )
            )

        if not getattr(
                response,
                "content",
                b"",
        ):
            return {}

        data = response.json()

        return (
            data
            if isinstance(
                data,
                dict,
            )
            else {
                "data": data
            }
        )

    @staticmethod
    def _feature_number(feature_number: str) -> str:
        feature = str(feature_number)
        if not feature.isascii() or not feature.isdigit():
            raise ValueError("Feature number must contain ASCII digits only.")
        return feature

    @staticmethod
    def _has_feature_tag(task: Dict[str, Any], feature: str) -> bool:
        return any(str(tag.get("name", "")).strip() == feature for tag in task.get("tags", [])
                   if isinstance(tag, dict))

    def get_task(self, task_id: str) -> Dict[str, Any]:
        task_id = str(task_id or "").strip()
        if not task_id:
            raise ValueError("ClickUp task ID is required.")
        return self.request("GET", "/task/" + quote(task_id, safe=""))

    def tasks_for_feature_tag(self, feature_number: str) -> list[Dict[str, Any]]:
        feature = self._feature_number(feature_number)
        matches = []
        page = 0
        while True:
            result = self.request("GET", "/list/%s/task" % quote(self.list_id, safe=""), params={
                "page": page,
                "include_closed": "true",
                "subtasks": "true",
                "tags[]": feature,
            })
            tasks = result.get("tasks", [])
            matches.extend(task for task in tasks if isinstance(task, dict) and self._has_feature_tag(task, feature))
            if result.get("last_page") is True or len(tasks) < 100:
                break
            page += 1
        return matches

    def resolve_task(
            self,
            feature_number: str,
            cached_task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Resolve one immutable feature task.

        When a task ID is supplied it is authoritative:
        validate that exact task and never fall back to
        another task.

        Exact-tag discovery is allowed only when no
        permanent task ID has been established yet.
        """
        feature = self._feature_number(
            feature_number
        )

        if cached_task_id:
            try:
                cached = self.get_task(
                    cached_task_id
                )

            except ClickUpError as exc:
                raise ClickUpError(
                    "Stored ClickUp task ID %s for "
                    "feature_%s could not be loaded; "
                    "automatic reassignment is forbidden."
                    % (
                        cached_task_id,
                        feature,
                    )
                ) from exc

            if not self._has_feature_tag(
                    cached,
                    feature,
            ):
                raise ClickUpError(
                    "Stored ClickUp task ID %s no longer "
                    "has exact feature tag %s; automatic "
                    "reassignment is forbidden."
                    % (
                        cached_task_id,
                        feature,
                    )
                )

            return cached

        tasks = self.tasks_for_feature_tag(
            feature
        )

        if not tasks:
            raise ClickUpTaskNotFound(
                "CLICKUP TASK NOT FOUND for feature_%s"
                % feature
            )

        if len(tasks) > 1:
            raise AmbiguousClickUpTask(
                "AMBIGUOUS CLICKUP TAG for "
                "feature_%s (%d tasks)"
                % (
                    feature,
                    len(tasks),
                )
            )

        return tasks[0]

    def validate_task_identity(
            self,
            task_id: str,
            feature_number: str,
    ) -> Dict[str, Any]:
        """
        Require the permanent task ID to exist and retain
        the exact feature-number tag.
        """
        return self.resolve_task(
            feature_number,
            task_id,
        )

    @staticmethod
    def task_status_name(
            task: Dict[str, Any],
    ) -> str:
        raw = task.get(
            "status",
            "",
        )

        if isinstance(raw, dict):
            raw = raw.get(
                "status",
                raw.get(
                    "name",
                    "",
                ),
            )

        return str(
            raw or ""
        ).strip()

    def set_task_status(
            self,
            task_id: str,
            status: str,
    ) -> Dict[str, Any]:
        status = str(
            status or ""
        ).strip()

        if not status:
            raise ValueError(
                "ClickUp task status is required."
            )

        return self.request(
            "PUT",
            "/task/"
            + quote(
                str(task_id),
                safe="",
            ),
            json={
                "status": status,
            },
        )

    def wait_for_task_status(
            self,
            task_id: str,
            expected_status: str,
            *,
            feature_number: Optional[str] = None,
            changed_from_date_updated: Optional[str] = None,
            attempts: int = 30,
            delay_seconds: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Poll one immutable task until its status and
        recurrence mutation verify.
        """
        expected = str(
            expected_status or ""
        ).strip().casefold()

        if not expected:
            raise ValueError(
                "Expected ClickUp task status is required."
            )

        last_task = None

        for attempt in range(
                max(
                    1,
                    int(attempts),
                )
        ):
            task = self.get_task(
                task_id
            )
            last_task = task

            if feature_number is not None:
                feature = self._feature_number(
                    feature_number
                )

                if not self._has_feature_tag(
                        task,
                        feature,
                ):
                    raise ClickUpError(
                        "Permanent ClickUp task lost "
                        "exact feature tag %s during "
                        "status verification."
                        % feature
                    )

            status_matches = (
                    self.task_status_name(
                        task
                    ).casefold()
                    == expected
            )

            mutation_matches = True

            if (
                    changed_from_date_updated
                    is not None
            ):
                mutation_matches = (
                        str(
                            task.get(
                                "date_updated",
                                "",
                            )
                            or ""
                        )
                        != str(
                    changed_from_date_updated
                )
                )

            if (
                    status_matches
                    and mutation_matches
            ):
                return task

            if (
                    attempt + 1
                    < max(
                1,
                int(attempts),
            )
            ):
                time.sleep(
                    max(
                        0.0,
                        float(
                            delay_seconds
                        ),
                    )
                )

        actual = self.task_status_name(
            last_task or {}
        )

        if (
                changed_from_date_updated
                is not None
        ):
            raise ClickUpError(
                "ClickUp recurrence could not be "
                "verified: expected status '%s' and "
                "a changed date_updated value, got "
                "status '%s'."
                % (
                    expected_status,
                    actual,
                )
            )

        raise ClickUpError(
            "ClickUp task status did not become '%s'; "
            "last observed status was '%s'."
            % (
                expected_status,
                actual,
            )
        )

    def update_task_fields(self, task_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Update only supported non-description task fields."""
        allowed = {"name", "start_date", "start_date_time", "due_date", "due_date_time"}
        if not fields or set(fields) - allowed:
            raise ValueError("Task updates may contain only name and start/due date fields.")
        return self.request("PUT", "/task/" + quote(str(task_id), safe=""), json=fields)

    def set_custom_field(self, task_id: str, field_id: str, value: Any) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/task/%s/field/%s" % (quote(str(task_id), safe=""), quote(str(field_id), safe="")),
            json={"value": value, "value_options": {"time": False}},
        )

    def upload_task_attachment(self, task_id: str, file_path: Path | str) -> Dict[str, Any]:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if path.stat().st_size > 1024 * 1024 * 1024:
            raise ClickUpError("ClickUp attachment exceeds the documented 1 GB limit.")
        headers = dict(getattr(self.session, "headers", {}))
        headers.pop("Content-Type", None)
        with path.open("rb") as stream:
            result = self.request(
                "POST",
                "/task/%s/attachment" % quote(str(task_id), safe=""),
                headers=headers,
                files={"attachment": (path.name, stream, "application/octet-stream")},
            )
        return result

    def task_attachments(self, task_id: str) -> list[Dict[str, Any]]:
        task = self.get_task(task_id)
        attachments = task.get("attachments", []) or []
        return [item for item in attachments if isinstance(item, dict)]

    def upload_and_verify_attachment(self, task_id: str, file_path: Path | str, *, before_upload=None) -> Dict[str, Any]:
        path = Path(file_path)
        before = self.task_attachments(task_id)
        before_ids = {str(item.get("id")) for item in before if item.get("id") is not None}
        if before_upload is not None:
            before_upload()
        uploaded = self.upload_task_attachment(task_id, path)
        after = self.task_attachments(task_id)
        payload = uploaded.get("attachment", uploaded)
        uploaded_id = payload.get("id") if isinstance(payload, dict) else None
        verified = None
        if uploaded_id is not None:
            verified = next((item for item in after if str(item.get("id")) == str(uploaded_id)), None)
        if verified is None:
            new_items = [item for item in after if item.get("id") is not None and str(item.get("id")) not in before_ids]
            verified = next((item for item in new_items
                             if str(item.get("title") or item.get("name") or "") == path.name), None)
        if verified is None:
            verified = next((item for item in reversed(after)
                             if str(item.get("title") or item.get("name") or "") == path.name
                             and int(item.get("size") or -1) == path.stat().st_size), None)
        if verified is None:
            raise ClickUpError("Attachment upload could not be verified by reading the target task.")
        managed_name = re.compile(r"^\d+\.sqlite$", re.IGNORECASE)
        old_managed = any(
            str(item.get("title") or item.get("name") or "") == path.name
            and (item.get("id") is None or str(item.get("id")) != str(verified.get("id")))
            for item in after
        )
        return {"attachment": verified, "old_managed_attachment_remains": bool(old_managed and managed_name.match(path.name))}

    def create_completed_time_entry(
        self, team_id: str, task_id: str, start_ms: int, end_ms: int, description: str = "",
    ) -> Dict[str, Any]:
        if int(end_ms) <= int(start_ms):
            raise ValueError("Completed time entry end must be after its start.")
        return self.request("POST", "/team/%s/time_entries" % quote(str(team_id), safe=""), json={
            "tid": str(task_id), "start": int(start_ms), "end": int(end_ms),
            "duration": int(end_ms) - int(start_ms),
            "description": str(description),
        })

    def get_time_entries(self, team_id: str, start_ms: int, end_ms: int, task_id: Optional[str] = None):
        params = {"start_date": int(start_ms), "end_date": int(end_ms)}
        if task_id:
            params["task_id"] = str(task_id)
        return self.request("GET", "/team/%s/time_entries" % quote(str(team_id), safe=""), params=params)

    def find_time_entry_by_marker(self, team_id: str, start_ms: int, end_ms: int,
                                  task_id: str, marker: str) -> Optional[Dict[str, Any]]:
        result = self.get_time_entries(team_id, start_ms, end_ms, task_id)
        entries = result.get("data", result.get("time_entries", result.get("entries", [])))
        if isinstance(entries, dict):
            entries = entries.get("data", entries.get("time_entries", []))
        matches = [entry for entry in entries if isinstance(entry, dict)
                   and str(entry.get("description", "")).split("|", 1)[0].strip() == marker]
        if len(matches) > 1:
            raise ClickUpError("Multiple ClickUp time entries contain the same local interval marker.")
        return matches[0] if matches else None

    @staticmethod
    def verify_time_entry(entry: Dict[str, Any], task_id: str, start_ms: int, end_ms: int, marker: str) -> Dict[str, Any]:
        """Require exact task, marker, and millisecond bounds before accepting a remote entry."""
        description = str(entry.get("description", ""))
        task = entry.get("task")
        returned_task_id = entry.get("task_id")
        if isinstance(task, dict):
            returned_task_id = task.get("id", returned_task_id)
        try:
            returned_start = int(entry["start"])
            returned_end = int(entry.get("end", entry.get("stop")))
        except (KeyError, TypeError, ValueError):
            raise ClickUpError("ClickUp time-entry read-back omitted valid start/end timestamps.")
        if description.split("|", 1)[0].strip() != marker:
            raise ClickUpError("ClickUp time-entry marker does not match the local interval.")
        if returned_task_id is None or str(returned_task_id) != str(task_id):
            raise ClickUpError("ClickUp time-entry task does not match the selected feature task.")
        if returned_start != int(start_ms) or returned_end != int(end_ms):
            raise ClickUpError("ClickUp time-entry timestamps do not match the local interval.")
        return entry

    @staticmethod
    def _utc_calendar_date_from_ms(value: Any):
        try:
            from datetime import datetime, timezone

            return datetime.fromtimestamp(
                int(value) / 1000.0,
                tz=timezone.utc,
            ).date()

        except (
            TypeError,
            ValueError,
            OSError,
            OverflowError,
        ):
            return None

    def verify_task_dates(
        self,
        task_id: str,
        start_ms: int,
        due_ms: int,
        custom_field_id: Optional[str] = None,
        custom_field_value: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Verify calendar dates only.

        Time-of-day is intentionally ignored.
        """
        task = self.get_task(task_id)

        expected_start = self._utc_calendar_date_from_ms(
            start_ms
        )
        expected_due = self._utc_calendar_date_from_ms(
            due_ms
        )

        actual_start = self._utc_calendar_date_from_ms(
            task.get("start_date")
        )
        actual_due = self._utc_calendar_date_from_ms(
            task.get("due_date")
        )

        if actual_start != expected_start:
            raise ClickUpError(
                "ClickUp task start-date read-back did not "
                "match the requested calendar date."
            )

        if actual_due != expected_due:
            raise ClickUpError(
                "ClickUp task due-date read-back did not "
                "match the requested calendar date."
            )

        if custom_field_id is not None:
            fields = task.get("custom_fields", []) or []

            field = next(
                (
                    item
                    for item in fields
                    if str(item.get("id"))
                    == str(custom_field_id)
                ),
                None,
            )

            actual_custom = (
                self._utc_calendar_date_from_ms(
                    field.get("value")
                )
                if field
                else None
            )

            expected_custom = (
                self._utc_calendar_date_from_ms(
                    custom_field_value
                )
            )

            if (
                field is None
                or actual_custom != expected_custom
            ):
                raise ClickUpError(
                    "ClickUp custom-field read-back did not "
                    "match the requested calendar date."
                )

        return task
