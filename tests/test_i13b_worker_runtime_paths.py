"""I13B regressions: checkout compatibility, installed isolation and overrides."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from src.automator.infrastructure.runtime_config import (
    DATA_ROOT_ENV,
    SCRIPT_PYTHON_ENV,
    WORKSPACE_ENV,
    RuntimeSettings,
    save_runtime_settings,
    worker_runtime_settings,
    worker_workspace,
)


def test_checkout_defaults_preserve_data_and_feature_locations(monkeypatch):
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    monkeypatch.delenv(SCRIPT_PYTHON_ENV, raising=False)
    src = Path(__file__).resolve().parents[1]
    module = src / "src/automator/workers/pack_features_worker.py"
    settings = worker_runtime_settings(module)
    assert settings.data_root == src
    assert worker_workspace(settings) == src.parent


def test_installed_module_never_writes_next_to_package(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    site = tmp_path / "installed" / "site-packages" / "automator" / "workers" / "runner.py"
    site.parent.mkdir(parents=True)
    site.write_text("# installed")
    settings = worker_runtime_settings(site)
    assert settings.data_root != site.parent
    assert worker_workspace(settings) == settings.data_root / "features"
    assert not settings.data_root.exists()


def test_explicit_data_root_and_workspace_overrides(tmp_path, monkeypatch):
    custom_data = tmp_path / "data-root"
    custom_features = tmp_path / "scripts"
    monkeypatch.setenv(DATA_ROOT_ENV, str(custom_data))
    monkeypatch.setenv(WORKSPACE_ENV, str(custom_features))
    settings = worker_runtime_settings(__file__)
    assert settings.data_root == custom_data.resolve()
    assert worker_workspace(settings) == custom_features.resolve()
    assert not custom_data.exists()
    assert not custom_features.exists()


def test_saved_workspace_is_used_by_worker(tmp_path, monkeypatch):
    root = tmp_path / "automator-data"
    chosen = tmp_path / "feature-folder"
    save_runtime_settings(RuntimeSettings(data_root=root, workspace_dir=chosen))
    monkeypatch.setenv(DATA_ROOT_ENV, str(root))
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    settings = worker_runtime_settings(__file__)
    assert worker_workspace(settings) == chosen.resolve()


def test_worker_modules_apply_environment_overrides_in_fresh_process(tmp_path):
    root = tmp_path / "data-root"
    workspace = tmp_path / "features"
    code = (
        "from pathlib import Path; "
        "from src.automator.workers import pack_features_worker as p, "
        "copy_packed_zips_worker as c, clean_feature_directories_worker as d, "
        "batch_outputs_worker as b, run_features_worker as r; "
        "from src.automator.integrations.clickup import sync_clickup_task_ids as s; "
        "assert all(x.DEFAULT_SCRIPT_RUNS_DIR == Path(%r) for x in (p,c,d,b,r)); "
        "assert b.DEFAULT_OUTPUTS_ROOT == Path(%r) / 'Outputs'; "
        "assert r.STORAGE.root == s.STORAGE.root == b.STORAGE.root == Path(%r); "
        "assert s.RECORDS_JSON == Path(%r) / 'data' / 'records.json'; "
        "assert r.RUNTIME_SETTINGS.script_python == Path(%r).resolve()"
    ) % (str(workspace), str(root), str(root), str(root), sys.executable)
    env = dict(os.environ)
    env[DATA_ROOT_ENV] = str(root)
    env[WORKSPACE_ENV] = str(workspace)
    env[SCRIPT_PYTHON_ENV] = sys.executable
    project = Path(__file__).resolve().parents[1]
    completed = subprocess.run([sys.executable, "-c", code], cwd=project, env=env,
                               text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not root.exists()
    assert not workspace.exists()
