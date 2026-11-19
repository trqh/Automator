"""Regression: first-run feature registration runs after canonical setup."""
from automator.infrastructure.runtime_config import RuntimeSettings
from automator.infrastructure.canonical_paths import canonical_paths
from automator.services import first_run


def test_example_installation_after_both_canonical_directories_exist(tmp_path, monkeypatch):
    settings = RuntimeSettings(data_root=tmp_path / "fresh")
    actual_install = first_run.install_first_run_example
    called = []

    def checked_install(workspace):
        paths = canonical_paths(settings.data_root, workspace)
        assert paths.outputs.is_dir(), "canonical output directory missing during install"
        assert paths.history.is_dir(), "canonical history directory missing during install"
        called.append(workspace)
        return actual_install(workspace)

    monkeypatch.setattr(first_run, "install_first_run_example", checked_install)
    result = first_run.initialize_first_run(settings)
    assert called == [result.workspace_path]
