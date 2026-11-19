import copy

import pytest

from src.automator.integrations.clickup import sync_clickup_task_ids as SYNC


def _task(
    *,
    feature="123",
    task_id="task-1",
    frequency="Weekly",
    status="In Progress",
):
    return {
        "id": task_id,
        "tags": [{"name": feature}],
        "status": {"status": status},
        "custom_fields": [
            {
                "name": "Update frequency",
                "value": frequency,
            }
        ],
    }


def _run_sync(
    monkeypatch,
    *,
    initial_records,
    current_records,
    task,
):
    loads = iter([
        copy.deepcopy(initial_records),
        copy.deepcopy(current_records),
    ])
    captured = {}

    monkeypatch.setattr(
        SYNC,
        "API_KEY",
        "test-token",
    )
    monkeypatch.setattr(
        SYNC,
        "CLICKUP_LIST_ID",
        "test-list",
    )
    monkeypatch.setattr(
        SYNC,
        "load_records",
        lambda: next(loads),
    )
    monkeypatch.setattr(
        SYNC,
        "scan_clickup_list",
        lambda _client: copy.deepcopy(
            task if isinstance(task, list) else [task]
        ),
    )
    monkeypatch.setattr(
        SYNC,
        "acquire_lock",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        SYNC,
        "release_lock",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        SYNC,
        "atomic_write_records",
        lambda records: captured.setdefault(
            "records",
            copy.deepcopy(records),
        ),
    )
    monkeypatch.setattr(
        SYNC,
        "_write_last_run",
        lambda report: captured.setdefault(
            "report",
            copy.deepcopy(report),
        ),
    )

    assert SYNC.main() == 0
    return captured


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("Complete", "Complete"),
        ("In Progress", "In Progress"),
        ("To do", "To do"),
        ("To be checked", "To be checked"),
        ("in progress", "In Progress"),
    ],
)
def test_clickup_status_is_canonicalized(
    raw_status,
    expected,
):
    assert SYNC._task_clickup_status(
        {
            "status": {
                "status": raw_status,
            }
        }
    ) == expected


def test_clickup_status_updates_existing_task(
    monkeypatch,
):
    records = [
        {
            "feature_number": "123",
            "clickup_task_id": "task-1",
            "frequency": "Weekly",
            "clickup_status": "To do",
        }
    ]

    captured = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=_task(
            status="In Progress",
        ),
    )

    assert (
        captured["records"][0]["clickup_status"]
        == "In Progress"
    )
    assert (
        captured["report"]["clickup_status_changed"]
        == ["123"]
    )


def test_invalid_clickup_status_preserves_stored_value(
    monkeypatch,
):
    records = [
        {
            "feature_number": "123",
            "clickup_task_id": "task-1",
            "frequency": "Weekly",
            "clickup_status": "To do",
        }
    ]

    captured = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=_task(
            frequency="Monthly",
            status="Blocked",
        ),
    )

    record = captured["records"][0]

    assert record["frequency"] == "Monthly"
    assert record["clickup_status"] == "To do"
    assert len(
        captured["report"]["clickup_status_issues"]
    ) == 1


def test_discovered_task_also_stores_clickup_status(
    monkeypatch,
):
    records = [
        {
            "feature_number": "123",
            "clickup_task_id": "",
            "frequency": "Weekly",
        }
    ]

    captured = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=_task(
            status="Complete",
        ),
    )

    record = captured["records"][0]

    assert record["clickup_task_id"] == "task-1"
    assert record["clickup_status"] == "Complete"

def _with_dataops_status(task, value, *, field_type="drop_down"):
    task = copy.deepcopy(task)

    field = {
        "name": "WorkflowStatus",
        "type": field_type,
        "value": value,
    }

    if field_type == "drop_down":
        field["type_config"] = {
            "options": [
                {
                    "id": "opt-1",
                    "name": "Analyzed",
                    "orderindex": 0,
                },
                {
                    "id": "opt-2",
                    "name": "Prepare for upload",
                    "orderindex": 1,
                },
            ]
        }

    task["custom_fields"].append(field)
    return task


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("opt-1", "Analyzed"),
        (1, "Prepare for upload"),
        ({"id": "opt-2"}, "Prepare for upload"),
    ],
)
def test_dataops_status_dropdown_resolution(value, expected):
    assert SYNC._task_dataops_status(
        _with_dataops_status(_task(), value)
    ) == expected


def test_dataops_status_updates_existing_record_and_preserves_local_fields(
    monkeypatch,
):
    records = [{
        "feature_number": "123",
        "clickup_task_id": "task-1",
        "status": "Analyzed",
        "notes": "retain",
        "bugs": "open",
        "frequency": "Weekly",
    }]

    result = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=_with_dataops_status(_task(), "opt-2"),
    )

    assert result["records"][0]["status"] == "Prepare for upload"
    assert result["records"][0]["notes"] == "retain"
    assert result["records"][0]["bugs"] == "open"
    assert result["report"]["dataops_status_changed"] == ["123"]


def test_dataops_status_on_newly_discovered_identity(monkeypatch):
    records = [{
        "feature_number": "123",
        "clickup_task_id": "",
        "status": "Analyzed",
    }]

    result = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=_with_dataops_status(_task(), "opt-2"),
    )

    assert result["records"][0]["clickup_task_id"] == "task-1"
    assert result["records"][0]["status"] == "Prepare for upload"


def test_invalid_dataops_status_does_not_erase_existing_value(monkeypatch):
    records = [{
        "feature_number": "123",
        "clickup_task_id": "task-1",
        "status": "Analyzed",
    }]

    result = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=_with_dataops_status(
            _task(frequency="Monthly"),
            "invalid-option",
        ),
    )

    assert result["records"][0]["status"] == "Analyzed"
    assert len(result["report"]["dataops_status_issues"]) == 1


def test_dataops_text_field_status():
    assert SYNC._task_dataops_status(
        _with_dataops_status(
            _task(),
            "  Resolved  ",
            field_type="text",
        )
    ) == "Resolved"


def test_concurrent_dataops_status_change_aborts_merge(monkeypatch):
    initial = [{
        "feature_number": "123",
        "clickup_task_id": "task-1",
        "status": "Analyzed",
    }]

    concurrent = copy.deepcopy(initial)
    concurrent[0]["status"] = "In Progress"

    with pytest.raises(RuntimeError, match="local status changed"):
        _run_sync(
            monkeypatch,
            initial_records=initial,
            current_records=concurrent,
            task=_with_dataops_status(_task(), "opt-2"),
        )


def test_dataops_status_updates_when_clickup_task_status_is_unchanged(
    monkeypatch,
):
    records = [{
        "feature_number": "123",
        "clickup_task_id": "task-1",
        "frequency": "Weekly",
        "clickup_status": "In Progress",
        "status": "Analyzed",
        "notes": "Keep this note",
        "bugs": "open",
    }]

    result = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=_with_dataops_status(
            _task(
                frequency="Weekly",
                status="In Progress",
            ),
            "opt-2",
        ),
    )

    record = result["records"][0]

    assert record["clickup_status"] == "In Progress"
    assert record["status"] == "Prepare for upload"
    assert record["frequency"] == "Weekly"
    assert record["notes"] == "Keep this note"
    assert record["bugs"] == "open"
    assert result["report"]["clickup_status_changed"] == []
    assert result["report"]["dataops_status_changed"] == ["123"]
    assert result["report"]["dataops_status_verified"] == ["123"]


def test_absent_feature_prepends_note_without_losing_existing_notes(
    monkeypatch,
):
    records = [{
        "feature_number": "123",
        "clickup_task_id": "",
        "notes": "Important existing note\nSecond line",
        "status": "Analyzed",
    }]

    result = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=[],
    )

    assert result["records"][0]["notes"] == (
        "Ignore. This task has been removed from the list\n"
        "Important existing note\nSecond line"
    )
    assert result["report"]["notes_marked_absent"] == ["123"]
    assert result["report"]["notes_restored"] == []


def test_absent_stored_task_is_flagged_but_identity_is_preserved(
    monkeypatch,
):
    records = [{
        "feature_number": "123",
        "clickup_task_id": "task-old",
        "notes": "Keep this",
    }]

    result = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=[],
    )

    assert result["records"][0]["clickup_task_id"] == "task-old"
    assert result["records"][0]["notes"] == (
        "Ignore. This task has been removed from the list\n"
        "Keep this"
    )
    assert result["report"]["counts"]["IDENTITY CONFLICT"] == 1
    assert result["report"]["notes_marked_absent"] == ["123"]


def test_absence_note_is_not_duplicated(monkeypatch):
    records = [{
        "feature_number": "123",
        "clickup_task_id": "task-old",
        "notes": (
            "Ignore. This task has been removed from the list\n"
            "Original note"
        ),
    }]

    result = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=[],
    )

    assert "records" not in result
    assert result["report"]["notes_marked_absent"] == []
    assert result["report"]["records_json_updated"] is False


def test_reappearing_verified_task_removes_only_managed_prefix(
    monkeypatch,
):
    records = [{
        "feature_number": "123",
        "clickup_task_id": "task-1",
        "frequency": "Weekly",
        "clickup_status": "In Progress",
        "status": "Analyzed",
        "notes": (
            "Ignore. This task has been removed from the list\n"
            "Important original note\nSecond line"
        ),
    }]

    result = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=_with_dataops_status(_task(), "opt-1"),
    )

    assert result["records"][0]["notes"] == (
        "Important original note\nSecond line"
    )
    assert result["report"]["notes_restored"] == ["123"]


def test_ambiguous_identity_does_not_change_notes(monkeypatch):
    records = [{
        "feature_number": "123",
        "clickup_task_id": "",
        "notes": "Keep unchanged",
    }]

    result = _run_sync(
        monkeypatch,
        initial_records=records,
        current_records=records,
        task=[
            _task(task_id="task-1"),
            _task(task_id="task-2"),
        ],
    )

    assert result["report"]["counts"]["AMBIGUOUS"] == 1
    assert result["report"]["notes_marked_absent"] == []
    assert result["report"]["notes_restored"] == []


def test_concurrent_note_change_aborts_without_writing(monkeypatch):
    initial = [{
        "feature_number": "123",
        "clickup_task_id": "task-old",
        "notes": "Original note",
    }]
    concurrent = copy.deepcopy(initial)
    concurrent[0]["notes"] = "Someone edited this during the sync"

    with pytest.raises(RuntimeError, match="notes changed"):
        _run_sync(
            monkeypatch,
            initial_records=initial,
            current_records=concurrent,
            task=[],
        )
