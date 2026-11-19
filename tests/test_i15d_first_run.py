"""I15D regression tests; no existing user records should be rewritten."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.automator.infrastructure.runtime_config import RuntimeSettings
from src.automator.services.first_run import FirstRunError, initialize_first_run


def test_empty_install_creates_records_metadata_and_workspace(tmp_path):
    root = tmp_path / "new_install"
    result = initialize_first_run(RuntimeSettings(data_root=root))
    assert json.loads(result.records_path.read_text(encoding="utf-8")) == []
    meta = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert meta["records_version"] == 0
    assert meta["writer"] == "AutomatorFirstRun"
    assert result.workspace_path == root / "features"
    assert result.workspace_path.is_dir()
    assert result.records_path in result.created


def test_is_idempotent_and_does_not_touch_existing_bytes(tmp_path):
    settings = RuntimeSettings(data_root=tmp_path / "app")
    first = initialize_first_run(settings)
    first.records_path.write_bytes(b'[{"feature_number": "2695"}]\n')
    first.metadata_path.write_bytes(b'{"records_version": 24, "private": true}\n')
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (first.records_path, first.metadata_path)]
    again = initialize_first_run(settings)
    after = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (first.records_path, first.metadata_path)]
    assert before == after
    assert again.created == ()


def test_existing_malformed_data_is_not_overwritten(tmp_path):
    data = tmp_path / "root" / "data"
    data.mkdir(parents=True)
    records = data / "records.json"
    records.write_bytes(b"not json\x00")
    result = initialize_first_run(RuntimeSettings(data_root=data.parent))
    assert records.read_bytes() == b"not json\x00"
    assert result.metadata_path.exists()


def test_respects_external_workspace_without_replacing_files(tmp_path):
    settings = RuntimeSettings(data_root=tmp_path / "storage", workspace_dir=tmp_path / "my scripts")
    settings.workspace_dir.mkdir()
    source = settings.workspace_dir / "main.py"
    source.write_text("print('stay')", encoding="utf-8")
    result = initialize_first_run(settings)
    assert result.workspace_path == settings.workspace_dir
    assert source.read_text(encoding="utf-8") == "print('stay')"


def test_workspace_is_not_under_application_code_directory(tmp_path):
    installed_code = tmp_path / "Program Files" / "automator"
    settings = RuntimeSettings(data_root=tmp_path / "UserData")
    result = initialize_first_run(settings)
    assert not result.workspace_path.is_relative_to(installed_code)
    assert not installed_code.exists()


def test_bad_workspace_path_fails_without_deleting_existing_data(tmp_path):
    settings = RuntimeSettings(data_root=tmp_path / "storage")
    result = initialize_first_run(settings)
    bad = tmp_path / "not_a_folder"
    bad.write_text("keep", encoding="utf-8")
    with pytest.raises(FirstRunError, match="workspace"):
        initialize_first_run(replace(settings, workspace_dir=bad))
    assert bad.read_text(encoding="utf-8") == "keep"
    assert result.records_path.exists()


def test_app_root_means_parent_of_data_directory(tmp_path):
    root = tmp_path / "Automator_Test"
    result = initialize_first_run(RuntimeSettings(data_root=root))
    assert result.records_path == root / "data" / "records.json"
    assert not (root / "data" / "data").exists()
