import json

import pytest

from automator.workers.feature_setup_worker import FeatureSetupError, initialize_feature, inspect_feature_setup


def test_inspection_reports_missing_partial_and_ready_states(tmp_path):
    assert inspect_feature_setup("12", tmp_path).state == "NOT INITIALIZED"
    folder = tmp_path / "feature_12"
    folder.mkdir()
    (folder / "main.py").write_text("# existing", encoding="utf-8")
    partial = inspect_feature_setup("12", tmp_path)
    assert partial.state == "PARTIAL SETUP"
    assert partial.missing_files == ("script.config",)
    ready = initialize_feature("12", tmp_path, fill_missing=True)
    assert ready.state == "READY"
    assert (folder / "main.py").read_text(encoding="utf-8") == "# existing"
    assert json.loads((folder / "script.config").read_text(encoding="utf-8"))["organization_codes"] == {}


def test_initialize_creates_missing_feature_files_without_overwrite(tmp_path):
    result = initialize_feature("123", tmp_path)
    assert result.state == "READY"
    folder = tmp_path / "feature_123"
    (folder / "main.py").write_text("user changes", encoding="utf-8")
    assert initialize_feature("123", tmp_path).state == "READY"
    assert (folder / "main.py").read_text(encoding="utf-8") == "user changes"


@pytest.mark.parametrize("feature", ["../12", "12/3", "１２", "", "abc"])
def test_feature_number_rejects_path_traversal_and_non_ascii(feature, tmp_path):
    with pytest.raises(FeatureSetupError):
        inspect_feature_setup(feature, tmp_path)


def test_partial_setup_requires_explicit_fill_confirmation(tmp_path):
    folder = tmp_path / "feature_7"
    folder.mkdir()
    with pytest.raises(FeatureSetupError, match="confirmation"):
        initialize_feature("7", tmp_path)


def test_non_file_required_path_is_not_replaced(tmp_path):
    folder = tmp_path / "feature_8"
    folder.mkdir()
    (folder / "main.py").mkdir()
    with pytest.raises(FeatureSetupError, match="regular file"):
        initialize_feature("8", tmp_path, fill_missing=True)
    assert (folder / "main.py").is_dir()

def test_packaged_templates_work_outside_repository(tmp_path, monkeypatch):
    from importlib.resources import files
    from automator.workers import feature_setup_worker as worker

    # Resource discovery must not rely on the current directory.
    monkeypatch.chdir(tmp_path)

    workspace = tmp_path / "external_workspace"
    workspace.mkdir()

    result = worker.initialize_feature("12345", workspace)

    assert result.state == "READY"

    resources = files("src.automator.templates")
    feature_dir = workspace / "feature_12345"

    assert (feature_dir / "main.py").read_bytes() == (
        resources.joinpath("feature_main.py").read_bytes()
    )
    assert (feature_dir / "script.config").read_bytes() == (
        resources.joinpath("script.config").read_bytes()
    )