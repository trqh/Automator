from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from src.automator.infrastructure.runtime_config import (
    DATA_ROOT_ENV,
    SCRIPT_PYTHON_ENV,
    WORKSPACE_ENV,
    RuntimeSettings,
    RuntimeSettingsError,
    default_data_root,
    load_runtime_settings,
    probe_script_python,
    save_runtime_settings,
    selected_script_python,
)


def test_legacy_paths_preserved_without_writes(tmp_path):
    root = tmp_path / "Automator"
    resolved = load_runtime_settings(legacy_app_root=root, environment={})
    assert resolved.data_root == root.resolve()
    assert resolved.workspace_dir == root.parent.resolve()
    assert resolved.storage.data == root / "data"
    assert resolved.storage.path("run_history") == root / "run_history.jsonl"
    assert not root.exists()


@pytest.mark.parametrize("platform,expected", [
    ("win32", "AppData/Roaming/Automator"),
    ("darwin", "Library/Application Support/Automator"),
    ("linux", ".local/share/automator"),
])
def test_platform_specific_writable_data_root(tmp_path, platform, expected):
    result = default_data_root(environment={}, platform=platform, home=tmp_path)
    assert result == tmp_path / Path(expected)
    assert not result.exists()


def test_environment_data_root_is_independent_of_package_path(tmp_path):
    chosen = tmp_path / "custom"
    resolved = load_runtime_settings(environment={DATA_ROOT_ENV: str(chosen)})
    assert resolved.data_root == chosen.resolve()
    assert resolved.workspace_dir is None
    assert not chosen.exists()


def test_settings_round_trip_and_atomic_write(tmp_path):
    root = tmp_path / "Automator"
    workspace = tmp_path / "features"
    selected = tmp_path / "python-bin"
    path = save_runtime_settings(RuntimeSettings(root, workspace, selected))
    assert path == root / "data" / "runtime_settings.json"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    loaded = load_runtime_settings(legacy_app_root=root, environment={})
    assert loaded.workspace_dir == workspace.resolve()
    assert loaded.script_python == selected.resolve()
    assert list(path.parent.glob("runtime_settings.json.*.tmp")) == []


def test_precedence_environment_over_saved_preferences(tmp_path):
    root = tmp_path / "Automator"
    save_runtime_settings(RuntimeSettings(root, tmp_path / "old", tmp_path / "old-python"))
    loaded = load_runtime_settings(legacy_app_root=root, environment={
        WORKSPACE_ENV: str(tmp_path / "new"),
        SCRIPT_PYTHON_ENV: str(tmp_path / "new-python"),
    })
    assert loaded.workspace_dir == tmp_path / "new"
    assert loaded.script_python == tmp_path / "new-python"


def test_bad_settings_fail_without_overwriting(tmp_path):
    root = tmp_path / "Automator"
    path = root / "data" / "runtime_settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 999}', encoding="utf-8")
    with pytest.raises(RuntimeSettingsError, match="schema"):
        load_runtime_settings(legacy_app_root=root, environment={})
    assert path.read_text(encoding="utf-8") == '{"version": 999}'


def test_relative_workspace_setting_rejected(tmp_path):
    path = tmp_path / "data" / "runtime_settings.json"
    path.parent.mkdir()
    path.write_text('{"version": 1, "workspace_dir": "relative/path"}', encoding="utf-8")
    with pytest.raises(RuntimeSettingsError, match="absolute"):
        load_runtime_settings(legacy_app_root=tmp_path, environment={})


def test_selected_interpreter_must_be_explicit_for_frozen(tmp_path):
    settings = RuntimeSettings(tmp_path)
    with pytest.raises(RuntimeSettingsError, match="external"):
        selected_script_python(settings, frozen=True)
    assert selected_script_python(settings, frozen=False, development_python=Path(sys.executable)) == Path(sys.executable).resolve()
    custom = tmp_path / "python"
    assert selected_script_python(RuntimeSettings(tmp_path, script_python=custom), frozen=True) == custom.resolve()


def test_probe_current_interpreter():
    version = probe_script_python(Path(sys.executable))
    assert version == '.'.join(map(str, sys.version_info[:3]))


def test_probe_rejects_nonexistent_executable(tmp_path):
    with pytest.raises(RuntimeSettingsError, match="does not exist"):
        probe_script_python(tmp_path / "missing-python")


def test_save_does_not_touch_existing_records(tmp_path):
    root = tmp_path / "Automator"
    record = root / "data" / "records.json"
    record.parent.mkdir(parents=True)
    record.write_bytes(b'{"existing": true}')
    save_runtime_settings(RuntimeSettings(root))
    assert record.read_bytes() == b'{"existing": true}'
