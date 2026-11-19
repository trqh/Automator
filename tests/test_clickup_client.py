import pytest

from automator.integrations.clickup.clickup_client import (
    AmbiguousClickUpTask,
    ClickUpClient,
    ClickUpError,
    ClickUpTaskNotFound,
)


class Response:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.ok = status < 400
        self.content = b"{}"
        self.text = ""

    def json(self):
        return self._data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(responses):
    session = Session(responses)
    return ClickUpClient("token", "list id", session=session, base_url="https://example.test"), session


def task(task_id, feature="12"):
    return {"id": task_id, "tags": [{"name": feature}]}


def test_resolve_exactly_one_tag_match():
    subject, session = client([Response({"tasks": [task("a"), {"id": "noise", "tags": []}]})])
    assert subject.resolve_task("12")["id"] == "a"
    assert session.calls[0][2]["params"]["tags[]"] == "12"


def test_resolve_not_found_and_ambiguous_tag():
    subject, _ = client([Response({"tasks": []})])
    with pytest.raises(ClickUpTaskNotFound):
        subject.resolve_task("12")
    subject, _ = client([Response({"tasks": [task("a"), task("b")]})])
    with pytest.raises(AmbiguousClickUpTask):
        subject.resolve_task("12")


def test_cached_task_is_read_first_and_stale_cache_does_not_reassign():
    subject, session = client([Response(task("cached"))])
    assert subject.resolve_task("12", "cached")["id"] == "cached"
    assert len(session.calls) == 1

    subject, session = client([
        Response({}, 404),
    ])

    with pytest.raises(
        ClickUpError,
        match="automatic reassignment is forbidden",
    ):
        subject.resolve_task("12", "stale")

    assert [call[0] for call in session.calls] == ["GET"]

def test_task_update_rejects_description_and_sends_only_allowlisted_fields():
    subject, session = client([Response({"id": "x"})])
    with pytest.raises(ValueError):
        subject.update_task_fields("x", {"description": "overwrite"})
    subject.update_task_fields("x", {"start_date": 12, "due_date": 13})
    assert session.calls[0][2]["json"] == {"start_date": 12, "due_date": 13}


def test_completed_time_entries_send_explicit_timestamps_and_preserve_overlap():
    subject, session = client([Response({"data": {"id": "entry-1"}}), Response({"data": []})])
    result = subject.create_completed_time_entry("team", "task", 1000, 3000, "Debug")
    assert result["data"]["id"] == "entry-1"
    assert session.calls[0][2]["json"] == {
        "tid": "task", "start": 1000, "end": 3000, "duration": 2000, "description": "Debug",
    }
    subject.get_time_entries("team", 1000, 5000, "task")
    assert session.calls[1][2]["params"] == {"start_date": 1000, "end_date": 5000, "task_id": "task"}
    with pytest.raises(ValueError):
        subject.create_completed_time_entry("team", "task", 5, 5)


def test_attachment_upload_reads_task_back_and_reports_superseded_managed_file(tmp_path):
    source = tmp_path / "12.sqlite"
    source.write_bytes(b"accepted sqlite")
    old = {"id": "old", "title": "12.sqlite", "size": 8}
    new = {"id": "new", "title": "12.sqlite", "size": source.stat().st_size}
    subject, session = client([
        Response({"id": "task", "attachments": [old]}),
        Response({"id": "new", "title": "12.sqlite", "size": source.stat().st_size}),
        Response({"id": "task", "attachments": [old, new]}),
    ])
    result = subject.upload_and_verify_attachment("task", source)
    assert result["attachment"]["id"] == "new"
    assert result["old_managed_attachment_remains"] is True
    assert session.calls[1][0] == "POST"
    assert "Content-Type" not in session.calls[1][2]["headers"]


def test_task_readback_checks_dates_and_custom_field():
    subject, _ = client([Response({
        "id": "task", "start_date": 10, "due_date": 20,
        "custom_fields": [{"id": "field", "value": 10}],
    })])
    assert subject.verify_task_dates("task", 10, 20, "field", 10)["id"] == "task"
    subject, _ = client([Response({"id": "task", "start_date": 10, "due_date": 20})])
    with pytest.raises(Exception, match="custom-field read-back"):
        subject.verify_task_dates("task", 10, 20, "field", 10)


def test_time_entry_marker_readback_supports_safe_retry_and_rejects_duplicates():
    marker = "FeatureManager interval interval-1"
    subject, _ = client([Response({"data": [{"id": "entry-1", "description": marker}]})])
    assert subject.find_time_entry_by_marker("team", 0, 1000, "task", marker)["id"] == "entry-1"
    subject, _ = client([Response({"data": [
        {"id": "entry-1", "description": marker},
        {"id": "entry-2", "description": marker},
    ]})])
    with pytest.raises(Exception, match="Multiple ClickUp time entries"):
        subject.find_time_entry_by_marker("team", 0, 1000, "task", marker)


def test_time_entry_readback_requires_exact_marker_task_and_millisecond_bounds():
    marker = "FeatureManager interval interval-1"
    entry = {
        "id": "entry-1", "description": marker + " | development", "start": "1000", "end": "2000",
        "task": {"id": "task-1"},
    }
    assert ClickUpClient.verify_time_entry(entry, "task-1", 1000, 2000, marker) is entry
    with pytest.raises(Exception, match="task does not match"):
        ClickUpClient.verify_time_entry(entry, "task-2", 1000, 2000, marker)
    with pytest.raises(Exception, match="timestamps do not match"):
        ClickUpClient.verify_time_entry(entry, "task-1", 1001, 2000, marker)
    entry["description"] = "prefix " + marker
    with pytest.raises(Exception, match="marker does not match"):
        ClickUpClient.verify_time_entry(entry, "task-1", 1000, 2000, marker)


def test_time_entry_marker_search_uses_exact_prefix_not_substring():
    marker = "FeatureManager interval interval-1"
    subject, _ = client([Response({"data": [{"id": "other", "description": "copied " + marker}]})])
    assert subject.find_time_entry_by_marker("team", 0, 1000, "task", marker) is None
