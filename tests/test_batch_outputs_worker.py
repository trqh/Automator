import json
import sqlite3

import pytest

from automator.workers.batch_outputs_worker import batch_outputs


def feature(tmp_path, feature_number, count=None, *, missing_table=False, invalid=False, category="Sanctions"):
    feature_dir = tmp_path / ("feature_" + str(feature_number))
    feature_dir.mkdir(parents=True)
    (feature_dir / "script.config").write_text(json.dumps({"rationale_type": category}), encoding="utf-8")
    sqlite_file = feature_dir / (str(feature_number) + ".sqlite")
    if invalid:
        sqlite_file.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(str(sqlite_file)) as connection:
            if not missing_table:
                connection.execute("CREATE TABLE person (id INTEGER)")
                if count:
                    connection.executemany("INSERT INTO person VALUES (?)", ((i,) for i in range(count)))
    return feature_dir, sqlite_file


@pytest.mark.parametrize("count,expected", [(9999, "Regular"), (10000, "Big Files"), (10001, "Big Files")])
def test_threshold_routing_preserves_business_category(tmp_path, monkeypatch, count, expected):
    runs = tmp_path / "runs"
    runs.mkdir()
    feature(runs, 123, count=count)
    outputs = tmp_path / "Automator" / "Outputs"
    metadata = tmp_path / "logs" / "batch_metadata.json"
    def simulated_atomic_replace(source, destination):
        from pathlib import Path
        Path(destination).write_bytes(Path(source).read_bytes())
        Path(source).unlink()
    monkeypatch.setattr("automator.workers.batch_outputs_worker.os.replace", simulated_atomic_replace)
    result = batch_outputs(["123"], runs, outputs, metadata_path=metadata)[0]
    assert result.status == "success"
    assert result.category == "Sanctions"
    assert result.person_count == count
    assert result.output_class == expected
    expected_folder = outputs / "Big Files" if expected == "Big Files" else outputs / "Sanctions"
    assert result.dest_path == str(expected_folder / "123.sqlite")
    persisted = json.loads(metadata.read_text(encoding="utf-8"))["123"]
    assert persisted["project_type"] == "Sanctions"
    assert persisted["person_count"] == count


@pytest.mark.parametrize("options", [{"missing_table": True}, {"invalid": True}])
def test_missing_or_unreadable_person_table_fails_without_routing(tmp_path, options):
    runs = tmp_path / "runs"
    runs.mkdir()
    _folder, source = feature(runs, 7, **options)
    outputs = tmp_path / "Outputs"
    result = batch_outputs(["7"], runs, outputs, metadata_path=tmp_path / "meta.json")[0]
    assert result.status == "failure"
    assert result.output_class == "REVIEW"
    assert result.dest_path is None
    assert source.exists()
    assert not list(outputs.glob("**/*.sqlite"))


def test_batch_does_not_modify_sqlite_candidate(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    _folder, source = feature(runs, 9, count=1)
    before = source.read_bytes()
    def simulated_atomic_replace(source, destination):
        from pathlib import Path
        Path(destination).write_bytes(Path(source).read_bytes())
        Path(source).unlink()
    monkeypatch.setattr("automator.workers.batch_outputs_worker.os.replace", simulated_atomic_replace)
    batch_outputs(["9"], runs, tmp_path / "Outputs", metadata_path=tmp_path / "meta.json")
    assert source.read_bytes() == before
