"""Static I14B wiring checks; no Tk display or external services required."""
import ast
from pathlib import Path


def _gui_tree():
    gui_file = Path(__file__).resolve().parents[1] / "src/automator/ui/feature_manager_gui.py"
    return ast.parse(gui_file.read_text(encoding="utf-8"))


def test_gui_exposes_interpreter_and_demo_methods():
    tree = _gui_tree()
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FeatureManagerGUI")
    names = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert {"_open_python_settings", "_run_bundled_demo", "_finish_bundled_demo"} <= names


def test_gui_uses_instance_settings_in_feature_runner():
    tree = _gui_tree()
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "run_features"]
    assert len(calls) >= 2
    for call in calls:
        runtime = next((k.value for k in call.keywords if k.arg == "runtime_settings"), None)
        assert isinstance(runtime, ast.Attribute)
        assert runtime.attr == "runtime_settings"
        assert isinstance(runtime.value, ast.Name) and runtime.value.id == "self"


def test_gui_demo_button_is_connected():
    tree = _gui_tree()
    sources = ast.unparse(tree)
    assert "text='Run Demo'" in sources
    assert "command=self._run_bundled_demo" in sources
    assert "text='Python Settings'" in sources
