from pathlib import Path
import pytest

from src.automator.infrastructure import bundled_runtime as br
from src.automator.infrastructure.runtime_config import RuntimeSettingsError


def test_development_uses_host_interpreter(tmp_path):
    python = tmp_path / 'python.exe'
    assert br.runtime_python(frozen=False, gui_executable=python) == python.resolve()


def test_frozen_uses_separate_windows_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv('AUTOMATOR_BUNDLED_PYTHON', raising=False)
    python = tmp_path / 'runtime' / 'python.exe'
    python.parent.mkdir()
    python.touch()
    command = br.helper_command('automator.integrations.clickup.sync_clickup_task_ids',
                                frozen=True, gui_executable=tmp_path / 'Automator.exe',
                                platform='win32')
    assert command == [str(python), '-m', 'automator.integrations.clickup.sync_clickup_task_ids']


def test_frozen_uses_separate_unix_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv('AUTOMATOR_BUNDLED_PYTHON', raising=False)
    python = tmp_path / 'runtime' / 'bin' / 'python3'
    python.parent.mkdir(parents=True)
    python.touch()
    assert br.runtime_python(frozen=True, gui_executable=tmp_path / 'Automator',
                             platform='linux') == python


def test_missing_runtime_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv('AUTOMATOR_BUNDLED_PYTHON', raising=False)
    with pytest.raises(RuntimeSettingsError, match='incomplete'):
        br.runtime_python(frozen=True, gui_executable=tmp_path / 'Automator.exe',
                          platform='win32')


def test_frozen_never_uses_gui_as_python(tmp_path, monkeypatch):
    gui = tmp_path / 'Automator.exe'
    gui.touch()
    monkeypatch.setenv('AUTOMATOR_BUNDLED_PYTHON', str(gui))
    with pytest.raises(RuntimeSettingsError, match='GUI executable'):
        br.runtime_python(frozen=True, gui_executable=gui)


def test_invalid_helper_module_rejected():
    with pytest.raises(ValueError, match='package module'):
        br.helper_command('malicious.module', frozen=False)


def test_interpreter_validation_uses_selected_runtime(tmp_path, monkeypatch):
    python = tmp_path / 'runtime' / 'python.exe'
    python.parent.mkdir()
    python.touch()
    monkeypatch.setattr(br, 'probe_script_python', lambda p: '3.12.9' if p == python else None)
    assert br.verify_bundled_runtime(frozen=True, gui_executable=tmp_path / 'Automator.exe',
                                     platform='win32') == '3.12.9'
