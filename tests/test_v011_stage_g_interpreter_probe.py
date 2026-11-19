from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.automator.infrastructure import runtime_config as rc


def test_timed_probe_of_current_interpreter():
    version, elapsed = rc.probe_script_python_timed(Path(sys.executable))
    assert version == ".".join(map(str, sys.version_info[:3]))
    assert elapsed >= 0
    assert rc.probe_script_python(Path(sys.executable)) == version


def test_probe_reports_timeout_with_elapsed_time(monkeypatch):
    def slow(*args, **kwargs):
        assert kwargs["timeout"] == 30.0
        raise subprocess.TimeoutExpired(args[0], 30.0)
    monkeypatch.setattr(rc.subprocess, "run", slow)
    with pytest.raises(rc.RuntimeSettingsError, match="TimeoutExpired") as exc:
        rc.probe_script_python_timed(Path(sys.executable))
    assert "Elapsed:" in str(exc.value)
    assert "30.0" in str(exc.value)


def test_probe_rejects_non_python_program_with_stderr(monkeypatch):
    def fake(*args, **kwargs):
        return SimpleNamespace(stdout="", stderr="bad launch", returncode=7)
    monkeypatch.setattr(rc.subprocess, "run", fake)
    with pytest.raises(rc.RuntimeSettingsError) as exc:
        rc.probe_script_python_timed(Path(sys.executable))
    assert "bad launch" in str(exc.value)
    assert "Exit code: 7" in str(exc.value)
    assert "Elapsed:" in str(exc.value)


def test_python_settings_uses_background_probe_and_ui_queue():
    path = Path(__file__).resolve().parents[1] / "src/automator/ui/feature_manager_gui.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    gui = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FeatureManagerGUI")
    method = next(n for n in gui.body if isinstance(n, ast.FunctionDef) and n.name == "_open_python_settings")
    source = ast.unparse(method)
    assert "threading.Thread" in source
    assert "probe_script_python_timed(candidate)" in source
    assert "self._ui_post(finish_selection" in source
    assert "window.winfo_exists()" in source
