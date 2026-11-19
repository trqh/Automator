import json

import pytest

from automator.integrations.clickup.clickup_state import ClickUpStateStore
from automator.integrations.clickup.clickup_client import load_task_id_cache, save_task_id_cache


def test_sync_domains_are_independent_and_state_round_trips(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "clickup_sync_state.json"
    def simulated_replace(source, destination):
        from pathlib import Path
        Path(destination).write_bytes(Path(source).read_bytes())
        Path(source).unlink()
    monkeypatch.setattr("automator.integrations.clickup.clickup_state.os.replace", simulated_replace)
    store = ClickUpStateStore(path)
    store.update_feature("12", "task_sync", {"status": "SYNCED", "clickup_task_id": "task-1"})
    store.update_feature("12", "attachment_sync", {"status": "CURRENT"})
    assert store.read()["12"] == {
        "task_sync": {"status": "SYNCED", "clickup_task_id": "task-1"},
        "attachment_sync": {"status": "CURRENT"},
    }
    assert json.loads(path.read_text(encoding="utf-8")) == store.read()


def test_sync_state_rejects_invalid_domain_and_feature(tmp_path):
    store = ClickUpStateStore(tmp_path / "state.json")
    with pytest.raises(ValueError):
        store.update_feature("12", "all_sync", {})
    with pytest.raises(ValueError):
        store.update_feature("../12", "task_sync", {})


def test_task_id_cache_uses_shared_clickup_state_file(tmp_path, monkeypatch):
    path = tmp_path / "clickup_sync_state.json"
    def simulated_replace(source, destination):
        from pathlib import Path
        Path(destination).write_bytes(Path(source).read_bytes())
        Path(source).unlink()
    monkeypatch.setattr("automator.integrations.clickup.clickup_client.os.replace", simulated_replace)
    path.write_text(json.dumps({"12": {"attachment_sync": {"status": "CURRENT"}}}), encoding="utf-8")
    save_task_id_cache(path, {"12": "task-12", "15": "task-15"})
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["12"]["attachment_sync"]["status"] == "CURRENT"
    assert state["12"]["task_sync"]["clickup_task_id"] == "task-12"
    assert load_task_id_cache(path) == {"12": "task-12", "15": "task-15"}


def test_attachment_claim_prevents_duplicate_and_requires_manual_release(tmp_path, monkeypatch):
    def simulated_replace(source, destination):
        from pathlib import Path
        Path(destination).write_bytes(Path(source).read_bytes())
        Path(source).unlink()

    monkeypatch.setattr("automator.integrations.clickup.clickup_state.os.replace", simulated_replace)
    store = ClickUpStateStore(tmp_path / "clickup_sync_state.json")
    attempt = store.claim_attachment_upload("12", "task-1", "12.sqlite", "digest")
    assert attempt
    with pytest.raises(ValueError, match="unresolved outcome"):
        store.claim_attachment_upload("12", "task-1", "12.sqlite", "digest")
    store.update_feature("12", "attachment_sync", {"status": "UPLOAD UNKNOWN"})
    with pytest.raises(ValueError, match="Manual confirmation"):
        store.release_uncertain_attachment("12", attempt_id=attempt, confirmed_no_attachment=False)
    store.release_uncertain_attachment("12", attempt_id=attempt, confirmed_no_attachment=True)
    assert store.read()["12"]["attachment_sync"]["status"] == "NOT UPLOADED"
    assert store.claim_attachment_upload("12", "task-1", "12.sqlite", "digest")


def test_verified_attachment_upload_with_same_digest_is_already_complete(tmp_path, monkeypatch):
    def simulated_replace(source, destination):
        from pathlib import Path
        Path(destination).write_bytes(Path(source).read_bytes())
        Path(source).unlink()

    monkeypatch.setattr("automator.integrations.clickup.clickup_state.os.replace", simulated_replace)
    store = ClickUpStateStore(tmp_path / "clickup_sync_state.json")
    store.update_feature("12", "attachment_sync", {
        "status": "CURRENT", "source_path": "12.sqlite", "sha256": "digest", "attachment_id": "id-1",
    })
    assert store.claim_attachment_upload("12", "task-1", "12.sqlite", "digest") is None
