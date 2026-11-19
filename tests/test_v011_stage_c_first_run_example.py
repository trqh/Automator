"""Stage C: automatic and non-destructive first-run example registration."""
import json
from pathlib import Path

from automator.infrastructure.runtime_config import RuntimeSettings
from automator.services.first_run import initialize_first_run
from automator.workers.feature_setup_worker import inspect_feature_setup


def test_fresh_install_registers_and_initializes_ready_example(tmp_path):
    result = initialize_first_run(RuntimeSettings(data_root=tmp_path / "new"))
    records = json.loads(result.records_path.read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["feature_number"] == "900003"
    assert records[0]["clickup_task_id"] == ""
    assert inspect_feature_setup("900003", result.workspace_path).state == "READY"
    assert (result.workspace_path / "feature_900003" / "main.py").is_file()
    assert (result.workspace_path / "feature_900003" / "script.config").is_file()


def test_repeated_startup_does_not_replace_user_edits(tmp_path):
    settings = RuntimeSettings(data_root=tmp_path / "user")
    first = initialize_first_run(settings)
    main = first.workspace_path / "feature_900003" / "main.py"
    main.write_text("BASELINE_ONLY = False\n", encoding="utf-8")
    before = first.records_path.read_bytes()
    second = initialize_first_run(settings)
    assert second.created == ()
    assert main.read_text(encoding="utf-8") == "BASELINE_ONLY = False\n"
    assert first.records_path.read_bytes() == before


def test_existing_records_do_not_trigger_registration(tmp_path):
    root = tmp_path / "existing"
    data = root / "data"
    data.mkdir(parents=True)
    (data / "records.json").write_text('[{"feature_number": "42"}]', encoding="utf-8")
    initialize_first_run(RuntimeSettings(data_root=root))
    assert not (root / "features" / "feature_900003").exists()
    assert json.loads((data / "records.json").read_text(encoding="utf-8")) == [{"feature_number": "42"}]


def test_colliding_example_folder_is_preserved(tmp_path):
    root = tmp_path / "collision"
    example = root / "features" / "feature_900003"
    example.mkdir(parents=True)
    main = example / "main.py"
    main.write_bytes(b"custom source")
    result = initialize_first_run(RuntimeSettings(data_root=root))
    assert main.read_bytes() == b"custom source"
    assert not (example / "script.config").exists()
    assert json.loads(result.records_path.read_text(encoding="utf-8")) == []


def test_external_workspace_example_is_created_there(tmp_path):
    root = tmp_path / "data"
    external = tmp_path / "external features"
    result = initialize_first_run(RuntimeSettings(data_root=root, workspace_dir=external))
    assert inspect_feature_setup("900003", external).state == "READY"
    assert not (root / "features").exists()
