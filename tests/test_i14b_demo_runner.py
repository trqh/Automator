"""I14B: packaged demo runner and interpreter separation tests."""
from pathlib import Path
import sys

from src.automator.infrastructure.runtime_config import RuntimeSettings
from src.automator.services.demo_runner import bundled_demo_python, run_offline_demo


def test_unfrozen_demo_uses_current_python():
    assert bundled_demo_python(frozen=False) == Path(sys.executable).resolve()


def test_frozen_windows_demo_does_not_reuse_gui_executable(tmp_path):
    gui_exe = tmp_path / "Automator.exe"
    got = bundled_demo_python(frozen=True, executable=gui_exe, platform="win32")
    assert got == tmp_path / "runtime" / "python.exe"
    assert got != gui_exe


def test_environment_bundled_python_override(tmp_path, monkeypatch):
    path = tmp_path / "custom_python"
    monkeypatch.setenv("AUTOMATOR_BUNDLED_PYTHON", str(path))
    assert bundled_demo_python(frozen=True) == path


def test_offline_demo_works_without_external_python(tmp_path):
    settings = RuntimeSettings(data_root=tmp_path)
    outcome = run_offline_demo(settings, frozen=True, bundled_python=Path(sys.executable))
    assert outcome.succeeded, outcome.message
    assert outcome.output_database.is_file()
    assert outcome.run_directory.is_relative_to(tmp_path)
    assert (outcome.run_directory / "run_history.jsonl").exists()
    assert not (tmp_path / "data" / "records.json").exists()


def test_demo_runs_are_isolated(tmp_path):
    settings = RuntimeSettings(data_root=tmp_path)
    first = run_offline_demo(settings, bundled_python=Path(sys.executable))
    second = run_offline_demo(settings, bundled_python=Path(sys.executable))
    assert first.succeeded and second.succeeded
    assert first.run_directory != second.run_directory
