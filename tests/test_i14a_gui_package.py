"""I16B: GUI uses the installed automator package, not legacy root modules."""
from pathlib import Path
import importlib


def test_gui_package_import_identity():
    canonical = importlib.import_module("automator.ui.feature_manager_gui")
    assert canonical is importlib.import_module("automator.ui.feature_manager_gui")
    assert callable(canonical.main)
    assert canonical.FeatureManagerGUI is not None


def test_gui_worker_imports_are_canonical():
    gui = importlib.import_module("automator.ui.feature_manager_gui")
    from automator.workers.run_features_worker import run_features
    from automator.integrations.clickup.clickup_client import ClickUpClient
    assert gui.run_features is run_features
    assert gui.ClickUpClient is ClickUpClient


def test_gui_data_paths_match_configured_runtime():
    gui = importlib.import_module("automator.ui.feature_manager_gui")
    assert gui.STORAGE.root == gui.RUNTIME_SETTINGS.data_root
    assert gui.RECORDS_JSON == gui.STORAGE.data / "records.json"
    assert gui.DEFAULT_SCRIPT_RUNS_DIR == gui.RUNTIME_SETTINGS.workspace_dir or gui.RUNTIME_SETTINGS.workspace_dir is None


def test_clickup_sync_target_is_module_not_root_script():
    gui = importlib.import_module("automator.ui.feature_manager_gui")
    assert gui.SYNC_CLICKUP_TASK_IDS_MODULE.endswith(".integrations.clickup.sync_clickup_task_ids")
    assert "Sync ClickUp Task IDs.py" not in gui.SYNC_CLICKUP_TASK_IDS_MODULE
    assert importlib.util.find_spec(gui.SYNC_CLICKUP_TASK_IDS_MODULE) is not None
