"""I13: bundled demos run without external Python; user scripts remain separate."""
from pathlib import Path
import sys
import sqlite3
import shutil

import pytest

from src.automator.infrastructure.runtime_config import (
    RuntimeSettings, RuntimeSettingsError, resolve_feature_interpreter,
)
from src.automator.workers.run_features_worker import run_features


def test_frozen_demo_uses_bundled_interpreter_without_external_python(tmp_path):
    settings = RuntimeSettings(data_root=tmp_path)
    assert resolve_feature_interpreter(
        settings, is_demo=True, bundled_python=Path(sys.executable), frozen=True,
    ) == Path(sys.executable).resolve()


def test_frozen_regular_script_requires_external_python(tmp_path):
    with pytest.raises(RuntimeSettingsError, match="external Python"):
        resolve_feature_interpreter(
            RuntimeSettings(data_root=tmp_path),
            is_demo=False, bundled_python=Path(sys.executable), frozen=True,
        )


def test_frozen_selected_external_python_takes_precedence(tmp_path):
    chosen = tmp_path / "custom-python"
    assert resolve_feature_interpreter(
        RuntimeSettings(data_root=tmp_path, script_python=chosen),
        is_demo=True, bundled_python=Path(sys.executable), frozen=True,
    ) == chosen.resolve()


def test_missing_bundled_interpreter_is_clear_error(tmp_path):
    with pytest.raises(RuntimeSettingsError, match="Bundled demonstration Python not found"):
        resolve_feature_interpreter(
            RuntimeSettings(data_root=tmp_path), is_demo=True,
            bundled_python=tmp_path / "missing", frozen=True,
        )


def test_demo_runs_end_to_end_with_no_configured_python(tmp_path):
    demo_source = Path(__file__).resolve().parents[1] / (
        "src/automator/demo_scripts/feature_900001/main.py"
    )
    workspace = tmp_path / "workspace"
    target = workspace / "feature_900001"
    target.mkdir(parents=True)
    shutil.copy2(demo_source, target / "main.py")
    results = run_features(
        features=["900001"], script_runs_dir=workspace, logs_dir=tmp_path / "logs",
        runtime_settings=RuntimeSettings(data_root=tmp_path),
        demo_features=["900001"], bundled_python=Path(sys.executable),
        frozen=True, max_workers=1,
    )
    assert len(results) == 1
    assert results[0].status == "success", results[0].message
    with sqlite3.connect(target / "900001.sqlite") as con:
        assert con.execute("SELECT label FROM demo").fetchone()[0] == "Automator demo completed"


def test_bundled_demo_is_available_as_package_resource():
    from importlib.resources import files
    import json
    demo = files("src.automator.demo_scripts.feature_900001")
    assert "sqlite3" in demo.joinpath("main.py").read_text(encoding="utf-8")
    assert json.loads(demo.joinpath("script.config").read_text(encoding="utf-8"))["project_type"] == "Demo"
