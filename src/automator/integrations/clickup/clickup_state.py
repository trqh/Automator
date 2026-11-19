"""Atomic local state for ClickUp task, attachment, and time sync domains."""

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


class ClickUpStateStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def update_feature(
            self,
            feature_number,
            domain,
            values,
    ):
        if domain not in {
            "task_sync",
            "attachment_sync",
            "time_sync",
            "canonical_approval",
            "workflow_sync",
            "dataops_sync",
        }:
            raise ValueError(
                "Unknown ClickUp sync domain: %s"
                % domain
            )

        feature = str(
            feature_number
        )

        if (
                not feature.isascii()
                or not feature.isdigit()
        ):
            raise ValueError(
                "Feature number must contain "
                "ASCII digits only."
            )

        with self._lock:
            state = self.read()

            row = state.setdefault(
                feature,
                {},
            )

            if not isinstance(
                    row,
                    dict,
            ):
                row = state[feature] = {}

            current = row.setdefault(
                domain,
                {},
            )

            if not isinstance(
                    current,
                    dict,
            ):
                current = row[domain] = {}

            current.update(
                values
            )

            self._write(
                state
            )

            return state[
                feature
            ][domain]

    def replace_feature_domain(
            self,
            feature_number,
            domain,
            values,
    ):
        """
        Replace one feature domain atomically,
        discarding stale keys.
        """
        if domain not in {
            "task_sync",
            "attachment_sync",
            "time_sync",
            "canonical_approval",
            "workflow_sync",
            "dataops_sync",
        }:
            raise ValueError(
                "Unknown ClickUp sync domain: %s"
                % domain
            )

        feature = self._validate_feature(
            feature_number
        )

        if not isinstance(
                values,
                dict,
        ):
            raise ValueError(
                "ClickUp sync domain values "
                "must be a dict."
            )

        with self._lock:
            state = self.read()

            row = state.setdefault(
                feature,
                {},
            )

            if not isinstance(
                    row,
                    dict,
            ):
                row = state[feature] = {}

            row[domain] = dict(
                values
            )

            self._write(
                state
            )

            return row[domain]

    def claim_attachment_upload(self, feature_number, task_id, source_path, sha256):
        """Claim one file upload and refuse retries after an ambiguous submission."""
        feature = self._validate_feature(feature_number)
        with self._lock:
            state = self.read()
            row = state.setdefault(feature, {})
            if not isinstance(row, dict):
                row = state[feature] = {}
            domain = row.setdefault("attachment_sync", {})
            if not isinstance(domain, dict):
                domain = row["attachment_sync"] = {}
            if (domain.get("status") in {"CURRENT", "OLD ATTACHMENT REMAINS"}
                    and domain.get("sha256") == sha256
                    and domain.get("source_path") == str(source_path)
                    and domain.get("attachment_id")):
                return None
            if domain.get("status") in {"UPLOAD IN FLIGHT", "UPLOAD UNKNOWN"}:
                raise ValueError("Attachment upload has an unresolved outcome; verify ClickUp before retrying.")
            attempt_id = str(uuid.uuid4())
            domain.update({
                "status": "UPLOAD IN FLIGHT", "attempt_id": attempt_id,
                "clickup_task_id": str(task_id), "source_path": str(source_path),
                "sha256": str(sha256), "error": None,
                "last_attempt": datetime.now(timezone.utc).isoformat(),
            })
            self._write(state)
            return attempt_id

    def release_uncertain_attachment(self, feature_number, *, attempt_id, confirmed_no_attachment):
        """Permit another upload only after explicit confirmation that no upload remains or exists."""
        feature = self._validate_feature(feature_number)
        if not confirmed_no_attachment:
            raise ValueError("Manual confirmation is required to release an uncertain attachment.")
        with self._lock:
            state = self.read()
            row = state.get(feature, {})
            domain = row.get("attachment_sync", {}) if isinstance(row, dict) else {}
            if (not isinstance(domain, dict)
                    or domain.get("status") not in {"UPLOAD IN FLIGHT", "UPLOAD UNKNOWN"}
                    or domain.get("attempt_id") != attempt_id):
                raise ValueError("Attachment attempt is no longer current.")
            domain.update({
                "status": "NOT UPLOADED", "error": "Manually released after confirming no attachment exists.",
            })
            self._write(state)
            return domain

    @staticmethod
    def _validate_feature(feature_number):
        feature = str(feature_number)
        if not feature.isascii() or not feature.isdigit():
            raise ValueError("Feature number must contain ASCII digits only.")
        return feature

    def _write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
