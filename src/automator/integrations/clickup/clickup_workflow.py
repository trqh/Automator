"""Resumable ClickUp workflow for one permanent task per feature."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dateutil.relativedelta import relativedelta

from .clickup_client import ClickUpClient, ClickUpError
from .clickup_state import ClickUpStateStore


FREQUENCY_DELTAS = {
    "daily": relativedelta(days=1),
    "weekly": relativedelta(weeks=1),
    "monthly": relativedelta(months=1),
    "quarterly": relativedelta(months=3),
    "biannually": relativedelta(months=6),
    "annually": relativedelta(years=1),
}


@dataclass(frozen=True)
class ClickUpWorkflowItem:
    feature_number: str
    task_id: str
    canonical_path: Path
    canonical_sha256: str
    last_update_dt: datetime
    last_update_iso: str
    frequency: str


@dataclass
class ClickUpWorkflowResult:
    feature_number: str
    status: str
    clickup_task_id: str
    old_attachment_remains: bool = False
    dataops_ok: bool = False
    error: str = ""


def file_sha256(path: Path | str) -> str:
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def mark_canonical_approved(
    state_store: ClickUpStateStore,
    feature_number: str,
    canonical_path: Path | str,
    last_update_iso: str,
    *,
    source: str,
    digest: Optional[str] = None,
) -> str:
    path = Path(canonical_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    digest = str(digest or file_sha256(path))
    state_store.replace_feature_domain(
        feature_number,
        "canonical_approval",
        {
            "status": "APPROVED",
            "sha256": digest,
            "source": str(source),
            "canonical_path": str(path),
            "last_update_iso": str(last_update_iso or ""),
            "approved_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return digest


def mark_canonical_review_required(
    state_store: ClickUpStateStore,
    feature_number: str,
    comparison_state: str,
) -> None:
    state_store.replace_feature_domain(
        feature_number,
        "canonical_approval",
        {
            "status": "REVIEW REQUIRED",
            "comparison_state": str(comparison_state),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def canonical_approval_is_current(
    state_store: ClickUpStateStore,
    feature_number: str,
    canonical_path: Path | str,
    last_update_iso: str,
) -> tuple[bool, str]:
    row = state_store.read().get(str(feature_number), {})
    approval = row.get("canonical_approval", {}) if isinstance(row, dict) else {}
    if not isinstance(approval, dict) or approval.get("status") != "APPROVED":
        return False, "canonical SQLite has not been approved"

    if approval.get("last_update_iso") != str(last_update_iso or ""):
        return False, "SQLite approval belongs to an older successful run"

    path = Path(canonical_path)
    if not path.is_file():
        return False, "accepted canonical SQLite is missing"

    try:
        digest = file_sha256(path)
    except Exception as exc:
        return False, "could not hash canonical SQLite: %s" % exc

    if approval.get("sha256") != digest:
        return False, "canonical SQLite changed after approval"

    return True, ""


def invalidate_after_successful_run(
    state_store: ClickUpStateStore,
    feature_number: str,
    last_update_iso: str,
) -> None:
    feature = str(feature_number)
    state_store.replace_feature_domain(
        feature,
        "canonical_approval",
        {
            "status": "INVALIDATED",
            "reason": (
                "New successful run requires a fresh SQLite comparison "
                "or explicit acceptance."
            ),
            "invalidated_at": datetime.now(timezone.utc).isoformat(),
            "last_update_iso": str(last_update_iso or ""),
        },
    )
    state_store.replace_feature_domain(
        feature,
        "workflow_sync",
        {
            "status": "AWAITING SQLITE APPROVAL",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    attachment = (
        state_store.read()
        .get(feature, {})
        .get("attachment_sync", {})
    )
    if (
        not isinstance(attachment, dict)
        or attachment.get("status") not in {"UPLOAD IN FLIGHT", "UPLOAD UNKNOWN"}
    ):
        state_store.update_feature(
            feature,
            "attachment_sync",
            {"status": "STALE", "error": None},
        )

    state_store.update_feature(
        feature,
        "task_sync",
        {"status": "STALE", "error": None},
    )


def build_workflow_item(
    state_store: ClickUpStateStore,
    *,
    feature_number: str,
    last_status: str,
    frequency: str,
    last_update_dt: Optional[datetime],
    last_update_iso: str,
    task_id: str,
    canonical_path: Path | str,
) -> tuple[Optional[ClickUpWorkflowItem], str]:
    feature = str(feature_number)
    frequency = str(frequency or "").strip().lower()
    task_id = str(task_id or "").strip()
    canonical = Path(canonical_path)

    if str(last_status or "").strip().lower() != "success":
        return None, "last run status is not SUCCESS"
    if frequency not in FREQUENCY_DELTAS:
        return None, "frequency is missing or unsupported"
    if last_update_dt is None:
        return None, "last updated date is missing or invalid"
    if not task_id:
        return None, "no permanent ClickUp task ID is stored"

    approved, reason = canonical_approval_is_current(
        state_store,
        feature,
        canonical,
        last_update_iso,
    )
    if not approved:
        return None, reason

    return ClickUpWorkflowItem(
        feature_number=feature,
        task_id=task_id,
        canonical_path=canonical,
        canonical_sha256=file_sha256(canonical),
        last_update_dt=last_update_dt,
        last_update_iso=str(last_update_iso or ""),
        frequency=frequency,
    ), ""


def _workflow_custom_fields(
    client: ClickUpClient,
) -> tuple[str, str, object]:
    fields = client.request(
        "GET",
        "/list/%s/field" % client.list_id,
    ).get("fields", [])

    last_update_matches = [
        field
        for field in fields
        if str(
            field.get(
                "name",
                "",
            )
        ).strip().casefold()
        == "last update date"
    ]

    if (
        len(last_update_matches) != 1
        or last_update_matches[0].get("type")
        != "date"
    ):
        raise ClickUpError(
            "Expected exactly one ClickUp Date "
            "custom field named 'Last Update date'."
        )

    dataops_matches = [
        field
        for field in fields
        if str(
            field.get(
                "name",
                "",
            )
        ).strip().casefold()
        == "workflowstatus"
    ]

    if len(dataops_matches) != 1:
        raise ClickUpError(
            "Expected exactly one ClickUp custom "
            "field named 'WorkflowStatus'."
        )

    dataops_field = dataops_matches[0]
    dataops_type = str(
        dataops_field.get(
            "type",
            "",
        )
    ).strip()

    if dataops_type == "drop_down":
        type_config = (
            dataops_field.get(
                "type_config"
            )
            or {}
        )

        options = type_config.get(
            "options",
            [],
        )

        matches = [
            option
            for option in options
            if isinstance(
                option,
                dict,
            )
            and str(
                option.get(
                    "name",
                    "",
                )
            ).strip().casefold()
            == "prepare for upload"
        ]

        if (
            len(matches) != 1
            or not matches[0].get("id")
        ):
            raise ClickUpError(
                "WorkflowStatus dropdown must contain "
                "exactly one 'Prepare for upload' option."
            )

        dataops_value = str(
            matches[0]["id"]
        )

    elif dataops_type in {
        "short_text",
        "text",
    }:
        dataops_value = (
            "Prepare for upload"
        )

    else:
        raise ClickUpError(
            "ClickUp WorkflowStatus must be a "
            "drop_down, short_text, or text "
            "custom field; got '%s'."
            % dataops_type
        )

    return (
        str(
            last_update_matches[0]["id"]
        ),
        str(
            dataops_field["id"]
        ),
        dataops_value,
    )


def _verify_clickup_custom_field_value(
    client: ClickUpClient,
    task_id: str,
    field_id: str,
    expected_value,
) -> None:
    """
    Verify WorkflowStatus by its resolved ClickUp value.

    ClickUp dropdown writes use the option ID, but task
    read-back may represent the selected option by ID,
    orderindex, or another option-backed representation.
    """
    task = client.get_task(
        task_id
    )

    fields = (
        task.get(
            "custom_fields",
            [],
        )
        or []
    )

    field = next(
        (
            item
            for item in fields
            if str(
                item.get(
                    "id",
                    "",
                )
            )
            == str(field_id)
        ),
        None,
    )

    if field is None:
        raise ClickUpError(
            "ClickUp custom field read-back "
            "omitted WorkflowStatus."
        )

    field_type = str(
        field.get(
            "type",
            "",
        )
        or ""
    ).strip()

    raw_value = field.get(
        "value"
    )

    if field_type != "drop_down":
        if str(
            raw_value or ""
        ).strip().casefold() != str(
            expected_value or ""
        ).strip().casefold():
            raise ClickUpError(
                "ClickUp WorkflowStatus read-back "
                "did not match 'Prepare for upload'."
            )

        return

    type_config = (
        field.get(
            "type_config"
        )
        or {}
    )

    options = type_config.get(
        "options",
        [],
    )

    expected_option = next(
        (
            option
            for option in options
            if isinstance(
                option,
                dict,
            )
            and (
                str(
                    option.get(
                        "id",
                        "",
                    )
                )
                == str(expected_value)
                or str(
                    option.get(
                        "name",
                        "",
                    )
                ).strip().casefold()
                == "prepare for upload"
            )
        ),
        None,
    )

    if expected_option is None:
        raise ClickUpError(
            "Could not resolve the ClickUp "
            "'Prepare for upload' dropdown option "
            "during read-back verification."
        )

    expected_tokens = {
        str(
            expected_option.get(
                "id",
                "",
            )
        ).strip().casefold(),
        str(
            expected_option.get(
                "name",
                "",
            )
        ).strip().casefold(),
        str(
            expected_option.get(
                "orderindex",
                "",
            )
        ).strip().casefold(),
    }

    expected_tokens.discard(
        ""
    )

    actual_tokens = set()

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
            value = raw_value.get(
                key
            )

            if value is not None:
                actual_tokens.add(
                    str(
                        value
                    ).strip().casefold()
                )

    elif isinstance(
        raw_value,
        (
            list,
            tuple,
            set,
        ),
    ):
        for value in raw_value:
            if isinstance(
                value,
                dict,
            ):
                for key in (
                    "id",
                    "name",
                    "value",
                    "orderindex",
                ):
                    nested = value.get(
                        key
                    )

                    if nested is not None:
                        actual_tokens.add(
                            str(
                                nested
                            ).strip().casefold()
                        )

            elif value is not None:
                actual_tokens.add(
                    str(
                        value
                    ).strip().casefold()
                )

    elif raw_value is not None:
        actual_tokens.add(
            str(
                raw_value
            ).strip().casefold()
        )

    if not (
        actual_tokens
        & expected_tokens
    ):
        raise ClickUpError(
            "ClickUp WorkflowStatus read-back "
            "did not resolve to 'Prepare for upload'. "
            "Returned value: %r"
            % raw_value
        )

def _clickup_custom_field_display_name(
    task: dict,
    field_id: str,
) -> str:
    """
    Resolve the current human-readable value of one ClickUp custom field.

    Dropdown fields can be returned by ClickUp using an option ID,
    order index, name, or another option-backed representation.
    """
    fields = (
        task.get(
            "custom_fields",
            [],
        )
        or []
    )

    field = next(
        (
            item
            for item in fields
            if (
                isinstance(
                    item,
                    dict,
                )
                and str(
                    item.get(
                        "id",
                        "",
                    )
                )
                == str(
                    field_id
                )
            )
        ),
        None,
    )

    if field is None:
        raise ClickUpError(
            "ClickUp task read-back omitted "
            "the WorkflowStatus custom field."
        )

    raw_value = field.get(
        "value"
    )

    if raw_value is None:
        return ""

    field_type = str(
        field.get(
            "type",
            "",
        )
        or ""
    ).strip()

    if field_type != "drop_down":
        return str(
            raw_value
        ).strip()

    actual_tokens = set()

    def add_token(value) -> None:
        if value is None:
            return

        token = str(
            value
        ).strip().casefold()

        if token:
            actual_tokens.add(
                token
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
        for value in raw_value:
            if isinstance(
                value,
                dict,
            ):
                for key in (
                    "id",
                    "name",
                    "value",
                    "orderindex",
                ):
                    add_token(
                        value.get(
                            key
                        )
                    )
            else:
                add_token(
                    value
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

    matches = []

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

            if value is None:
                continue

            token = str(
                value
            ).strip().casefold()

            if token:
                option_tokens.add(
                    token
                )

        if (
            actual_tokens
            & option_tokens
        ):
            matches.append(
                option
            )

    if len(matches) == 1:
        return str(
            matches[0].get(
                "name",
                "",
            )
            or ""
        ).strip()

    if len(matches) > 1:
        raise ClickUpError(
            "ClickUp WorkflowStatus resolved "
            "to more than one dropdown option."
        )

    raise ClickUpError(
        "Could not resolve current ClickUp "
        "WorkflowStatus dropdown value: %r"
        % raw_value
    )

def _upload_approved_attachment(
    client: ClickUpClient,
    state_store: ClickUpStateStore,
    item: ClickUpWorkflowItem,
) -> tuple[str, str]:
    feature = item.feature_number
    task_id = item.task_id
    canonical = item.canonical_path

    task = client.validate_task_identity(task_id, feature)
    if str(task.get("id", "")) != task_id:
        raise ClickUpError(
            "ClickUp returned a different task ID; "
            "automatic reassignment is forbidden."
        )

    state_store.update_feature(
        feature,
        "task_sync",
        {"clickup_task_id": task_id},
    )

    attempt_id = state_store.claim_attachment_upload(
        feature,
        task_id,
        canonical,
        item.canonical_sha256,
    )
    if attempt_id is None:
        status = (
            state_store.read()
            .get(feature, {})
            .get("attachment_sync", {})
            .get("status", "CURRENT")
        )
        return str(status), "Already uploaded and verified"

    request_started = False
    try:
        def before_upload():
            nonlocal request_started
            request_started = True
            state_store.update_feature(
                feature,
                "attachment_sync",
                {
                    "status": "UPLOAD IN FLIGHT",
                    "request_started_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        upload = client.upload_and_verify_attachment(
            task_id,
            canonical,
            before_upload=before_upload,
        )
    except Exception as exc:
        status = "UPLOAD UNKNOWN" if request_started else "UPLOAD FAILED"
        state_store.update_feature(
            feature,
            "attachment_sync",
            {
                "status": status,
                "error": str(exc),
            },
        )
        raise

    attachment = upload["attachment"]
    status = (
        "OLD ATTACHMENT REMAINS"
        if upload["old_managed_attachment_remains"]
        else "CURRENT"
    )
    state_store.update_feature(
        feature,
        "attachment_sync",
        {
            "status": status,
            "attachment_id": str(attachment.get("id", "")),
            "file_name": canonical.name,
            "source_path": str(canonical),
            "sha256": item.canonical_sha256,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
            "attempt_id": None,
        },
    )
    return status, ""


def _run_clickup_stages(
    client: ClickUpClient,
    state_store: ClickUpStateStore,
    item: ClickUpWorkflowItem,
    last_update_field_id: str,
    dataops_status_field_id: str,
    dataops_status_value,
) -> ClickUpWorkflowResult:
    feature = item.feature_number
    task_id = item.task_id
    cycle_key = "%s|%s|%s" % (
        item.last_update_iso,
        item.canonical_sha256,
        task_id,
    )

    row = state_store.read().get(feature, {})
    workflow = row.get("workflow_sync", {}) if isinstance(row, dict) else {}
    if not isinstance(workflow, dict) or workflow.get("cycle_key") != cycle_key:
        state_store.replace_feature_domain(
            feature,
            "workflow_sync",
            {
                "cycle_key": cycle_key,
                "status": "IN PROGRESS",
                "recurrence_status": "PENDING",
                "attachment_status": "PENDING",
                "dates_status": "PENDING",
                "clickup_dataops_status": "PENDING",
                "dataops_status": "PENDING",
                "dataops_transition_required": None,
                "dataops_source_status": None,
                "clickup_task_id": task_id,
                "canonical_sha256": item.canonical_sha256,
                "last_update_iso": item.last_update_iso,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    task = client.validate_task_identity(task_id, feature)

    workflow = (
        state_store.read()
        .get(feature, {})
        .get("workflow_sync", {})
    )

    # Stage 1: fire recurrence once. A retry after the request was attempted
    # only verifies; it never sends Complete twice.
    if workflow.get("recurrence_status") != "VERIFIED":
        if not workflow.get("complete_requested"):
            before_updated = str(task.get("date_updated", "") or "")
            state_store.update_feature(
                feature,
                "workflow_sync",
                {
                    "recurrence_status": "COMPLETE IN FLIGHT",
                    "complete_requested": True,
                    "pre_complete_date_updated": before_updated,
                    "complete_requested_at": datetime.now(timezone.utc).isoformat(),
                    "error": None,
                },
            )
            try:
                client.set_task_status(task_id, "Complete")
            except Exception as exc:
                state_store.update_feature(
                    feature,
                    "workflow_sync",
                    {
                        "recurrence_status": "COMPLETE UNKNOWN",
                        "error": str(exc),
                    },
                )
                raise

            state_store.update_feature(
                feature,
                "workflow_sync",
                {
                    "recurrence_status": "COMPLETE SENT",
                    "complete_sent_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        else:
            before_updated = str(
                workflow.get("pre_complete_date_updated", "") or ""
            )
            if not before_updated:
                raise ClickUpError(
                    "A prior Complete request exists without its pre-request "
                    "verification marker; manual review is required."
                )

        workflow = (
            state_store.read()
            .get(feature, {})
            .get("workflow_sync", {})
        )
        before_updated = str(
            workflow.get("pre_complete_date_updated", "") or ""
        )

        try:
            task = client.wait_for_task_status(
                task_id,
                "In Progress",
                feature_number=feature,
                changed_from_date_updated=before_updated,
            )
        except Exception as exc:
            state_store.update_feature(
                feature,
                "workflow_sync",
                {
                    "recurrence_status": "VERIFY FAILED",
                    "error": str(exc),
                },
            )
            raise

        state_store.update_feature(
            feature,
            "workflow_sync",
            {
                "recurrence_status": "VERIFIED",
                "recurrence_verified_at": datetime.now(timezone.utc).isoformat(),
                "observed_status": client.task_status_name(task),
                "error": None,
            },
        )

    # Stage 2: approved canonical attachment.
    attachment_status, attachment_detail = _upload_approved_attachment(
        client,
        state_store,
        item,
    )
    if attachment_status not in {"CURRENT", "OLD ATTACHMENT REMAINS"}:
        raise ClickUpError(
            "Canonical attachment is not verified current."
        )
    state_store.update_feature(
        feature,
        "workflow_sync",
        {
            "attachment_status": attachment_status,
            "attachment_detail": attachment_detail,
        },
    )

    # Stage 3: update dates after recurrence and attachment verification.
    start_date = item.last_update_dt.date()
    due_date = start_date + FREQUENCY_DELTAS[item.frequency]
    start_ms = int(
        datetime(
            start_date.year,
            start_date.month,
            start_date.day,
            tzinfo=timezone.utc,
        ).timestamp()
        * 1000
    )
    due_ms = int(
        datetime(
            due_date.year,
            due_date.month,
            due_date.day,
            tzinfo=timezone.utc,
        ).timestamp()
        * 1000
    )

    task_domain = (
        state_store.read()
        .get(feature, {})
        .get("task_sync", {})
    )

    local_dates_current = (
            isinstance(task_domain, dict)
            and task_domain.get("status") == "SYNCED"
            and task_domain.get("cycle_key") == cycle_key
            and str(
        task_domain.get(
            "clickup_task_id",
            "",
        )
    )
            == task_id
            and task_domain.get(
        "start_date_ms"
    )
            == start_ms
            and task_domain.get(
        "due_date_ms"
    )
            == due_ms
            and task_domain.get(
        "custom_field_value"
    )
            == start_ms
    )

    dates_current = False

    if local_dates_current:
        try:
            client.verify_task_dates(
                task_id,
                start_ms,
                due_ms,
                last_update_field_id,
                start_ms,
            )

            dates_current = True

            state_store.update_feature(
                feature,
                "task_sync",
                {
                    "remote_verified_at": (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    ),
                    "error": None,
                },
            )

        except ClickUpError:
            # Local state still describes this cycle,
            # but ClickUp was changed externally.
            # Re-apply the expected dates below.
            dates_current = False

    if not dates_current:
        try:
            client.update_task_fields(
                task_id,
                {
                    "start_date": start_ms,
                    "start_date_time": False,
                    "due_date": due_ms,
                    "due_date_time": False,
                },
            )

            client.set_custom_field(
                task_id,
                last_update_field_id,
                start_ms,
            )

            client.verify_task_dates(
                task_id,
                start_ms,
                due_ms,
                last_update_field_id,
                start_ms,
            )

        except Exception as exc:
            state_store.update_feature(
                feature,
                "task_sync",
                {
                    "status": "FAILED",
                    "clickup_task_id": task_id,
                    "error": str(exc),
                },
            )

            state_store.update_feature(
                feature,
                "workflow_sync",
                {
                    "dates_status": "FAILED",
                    "error": str(exc),
                },
            )

            raise

        state_store.update_feature(
            feature,
            "task_sync",
            {
                "status": "SYNCED",
                "clickup_task_id": task_id,
                "cycle_key": cycle_key,
                "error": None,
                "last_synced_at": (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
                "remote_verified_at": (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
                "start_date_ms": start_ms,
                "due_date_ms": due_ms,
                "custom_field_value": start_ms,
            },
        )

    state_store.update_feature(
        feature,
        "workflow_sync",
        {
            "dates_status": "VERIFIED",
            "error": None,
        },
    )

    # Stage 4: inspect ClickUp WorkflowStatus.
    #
    # This is NOT a universal target-state stage.
    #
    # Only an observed ClickUp WorkflowStatus of "Analyzed" triggers
    # an update to "Prepare for upload" in ClickUp.
    #
    # Every other status is left unchanged.
    #
    # Once required, the transition remains durable for this
    # workflow cycle. A retry verifies the expected ClickUp value
    # rather than silently skipping an incomplete operation.
    #
    # The legacy DataOps website is not contacted.
    workflow = (
        state_store.read()
        .get(
            feature,
            {},
        )
        .get(
            "workflow_sync",
            {},
        )
    )

    transition_required = (
            isinstance(
                workflow,
                dict,
            )
            and workflow.get(
        "dataops_transition_required"
    )
            is True
    )

    if not transition_required:
        status_task = (
            client.validate_task_identity(
                task_id,
                feature,
            )
        )

        observed_dataops_status = (
            _clickup_custom_field_display_name(
                status_task,
                dataops_status_field_id,
            )
        )

        transition_required = (
                observed_dataops_status
                .strip()
                .casefold()
                == "analyzed"
        )

        if transition_required:
            state_store.update_feature(
                feature,
                "workflow_sync",
                {
                    "dataops_transition_required": True,
                    "dataops_source_status": (
                        observed_dataops_status
                    ),
                    "dataops_target_status": (
                        "Prepare for upload"
                    ),
                    "clickup_dataops_status": "PENDING",
                    "dataops_status": "PENDING",
                    "error": None,
                },
            )

        else:
            state_store.update_feature(
                feature,
                "workflow_sync",
                {
                    "dataops_transition_required": False,
                    "dataops_source_status": (
                        observed_dataops_status
                    ),
                    "dataops_target_status": None,
                    "clickup_dataops_status": (
                        "NOT REQUIRED"
                    ),
                    "dataops_status": (
                        "NOT REQUIRED"
                    ),
                    "error": None,
                },
            )

    dataops_ok = False

    if transition_required:
        workflow = (
            state_store.read()
            .get(
                feature,
                {},
            )
            .get(
                "workflow_sync",
                {},
            )
        )

        local_clickup_dataops_verified = (
                isinstance(
                    workflow,
                    dict,
                )
                and workflow.get(
            "clickup_dataops_status"
        )
                == "VERIFIED"
                and str(
            workflow.get(
                "clickup_dataops_field_id",
                "",
            )
        )
                == str(
            dataops_status_field_id
        )
                and str(
            workflow.get(
                "clickup_dataops_value",
                "",
            )
        )
                == str(
            dataops_status_value
        )
        )

        clickup_dataops_current = False

        if local_clickup_dataops_verified:
            try:
                _verify_clickup_custom_field_value(
                    client,
                    task_id,
                    dataops_status_field_id,
                    dataops_status_value,
                )

                clickup_dataops_current = True

                state_store.update_feature(
                    feature,
                    "workflow_sync",
                    {
                        "clickup_dataops_status": (
                            "VERIFIED"
                        ),
                        "clickup_dataops_verified_at": (
                            datetime.now(
                                timezone.utc
                            ).isoformat()
                        ),
                        "error": None,
                    },
                )

            except ClickUpError:
                # The transition remains required for this cycle.
                # If ClickUp was altered externally, restore the
                # required Prepare-for-upload value below.
                clickup_dataops_current = False

        if not clickup_dataops_current:
            try:
                client.request(
                    "POST",
                    "/task/%s/field/%s"
                    % (
                        task_id,
                        dataops_status_field_id,
                    ),
                    json={
                        "value": (
                            dataops_status_value
                        ),
                    },
                )

                _verify_clickup_custom_field_value(
                    client,
                    task_id,
                    dataops_status_field_id,
                    dataops_status_value,
                )

            except Exception as exc:
                state_store.update_feature(
                    feature,
                    "workflow_sync",
                    {
                        "clickup_dataops_status": (
                            "FAILED"
                        ),
                        "clickup_dataops_field_id": (
                            dataops_status_field_id
                        ),
                        "clickup_dataops_value": (
                            dataops_status_value
                        ),
                        "error": str(
                            exc
                        ),
                    },
                )

                raise

            state_store.update_feature(
                feature,
                "workflow_sync",
                {
                    "clickup_dataops_status": (
                        "VERIFIED"
                    ),
                    "clickup_dataops_field_id": (
                        dataops_status_field_id
                    ),
                    "clickup_dataops_value": (
                        dataops_status_value
                    ),
                    "clickup_dataops_verified_at": (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    ),
                    "error": None,
                },
            )

        # The ClickUp WorkflowStatus transition has been verified.
        # The legacy DataOps website is no longer part of this workflow.
        #
        # Retain the existing workflow state keys for compatibility
        # with previously persisted synchronization history.
        state_store.update_feature(
            feature,
            "workflow_sync",
            {
                "dataops_status": "NOT REQUIRED",
                "error": None,
            },
        )

        # Compatibility flag used by the GUI to persist the verified
        # Prepare for upload status in the local records.json.
        # It no longer signifies a successful legacy DataOps write.
        dataops_ok = True

    final_status = (
        "SUCCESS — DELETE OLD ATTACHMENT"
        if (
                attachment_status
                == "OLD ATTACHMENT REMAINS"
        )
        else "SUCCESS"
    )

    state_store.update_feature(
        feature,
        "workflow_sync",
        {
            "status": final_status,
            "clickup_ready": True,
            "completed_at": (
                datetime.now(
                    timezone.utc
                ).isoformat()
            ),
            "error": None,
        },
    )

    return ClickUpWorkflowResult(
        feature_number=feature,
        status=final_status,
        clickup_task_id=task_id,
        old_attachment_remains=(
                attachment_status
                == "OLD ATTACHMENT REMAINS"
        ),
        dataops_ok=dataops_ok,
    )

def sync_clickup_features(
    client: ClickUpClient,
    state_store: ClickUpStateStore,
    items: list[ClickUpWorkflowItem],
) -> list[ClickUpWorkflowResult]:
    """
    Run the ClickUp workflow independently for each feature.

    The ClickUp WorkflowStatus custom field changes from Analyzed
    to Prepare for upload only when that transition is required.
    The legacy DataOps website is never accessed.
    """
    (
        last_update_field_id,
        dataops_status_field_id,
        dataops_status_value,
    ) = _workflow_custom_fields(
        client
    )

    outcomes: list[
        ClickUpWorkflowResult
    ] = []

    for item in items:
        try:
            outcome = _run_clickup_stages(
                client,
                state_store,
                item,
                last_update_field_id,
                dataops_status_field_id,
                dataops_status_value,
            )

            outcomes.append(
                outcome
            )

        except Exception as exc:
            workflow = (
                state_store.read()
                .get(
                    item.feature_number,
                    {},
                )
                .get(
                    "workflow_sync",
                    {},
                )
            )

            stage = "FAILED"

            if isinstance(
                workflow,
                dict,
            ):
                for key in (
                    "dataops_status",
                    "clickup_dataops_status",
                    "dates_status",
                    "attachment_status",
                    "recurrence_status",
                ):
                    value = workflow.get(
                        key
                    )

                    if (
                        value
                        and value not in {
                            "PENDING",
                            "VERIFIED",
                            "CURRENT",
                            "SYNCED",
                            "NOT REQUIRED",
                        }
                    ):
                        stage = str(
                            value
                        )
                        break

            outcomes.append(
                ClickUpWorkflowResult(
                    feature_number=(
                        item.feature_number
                    ),
                    status="FAILED",
                    clickup_task_id=(
                        item.task_id
                    ),
                    error="%s - %s"
                    % (
                        stage,
                        exc,
                    ),
                )
            )

    return outcomes