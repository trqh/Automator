"""Guard that first-run setup is wired to the real GUI entry point."""
from __future__ import annotations

import ast
from pathlib import Path

GUI = Path(__file__).resolve().parents[1] / "src" / "automator" / "ui" / "feature_manager_gui.py"


def test_gui_main_initializes_before_constructing_window():
    tree = ast.parse(GUI.read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)]
    init = next(c for c in calls if isinstance(c.func, ast.Name) and c.func.id == "initialize_first_run")
    gui = next(c for c in calls if isinstance(c.func, ast.Name) and c.func.id == "FeatureManagerGUI")
    assert init.lineno < gui.lineno


def test_gui_imports_first_run_from_service():
    tree = ast.parse(GUI.read_text(encoding="utf-8"))
    assert any(
        isinstance(n, ast.ImportFrom)
        and n.module == "services.first_run"
        and n.level == 2
        and any(alias.name == "initialize_first_run" for alias in n.names)
        for n in tree.body
    )
