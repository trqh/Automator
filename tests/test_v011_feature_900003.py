"""Stage B packaged resource, deterministic generation and comparator tests."""
import json
import sqlite3
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from automator.workers.sqlite_compare_worker import (
    compare_feature,
    safe_replace_from_candidate,
)
from contextlib import closing
RESOURCE = "automator.demo_scripts.feature_900003"


def _installed_script(tmp_path: Path) -> Path:
    directory = tmp_path / "features" / "feature_900003"
    directory.mkdir(parents=True)
    for filename in ("main.py", "script.config"):
        (directory / filename).write_bytes(files(RESOURCE).joinpath(filename).read_bytes())
    return directory / "main.py"


def _run(script: Path):
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
                            text=True, timeout=15, check=True)
    assert "fictional people" in result.stdout
    return script.parent / "900003.sqlite"


def _people(database: Path):
    with closing(sqlite3.connect(database)) as connection, connection:
        people = connection.execute("SELECT id, name FROM person ORDER BY id").fetchall()
        identifiers = connection.execute(
            "SELECT person_id, value FROM identifiers ORDER BY person_id"
        ).fetchall()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    return people, identifiers


def test_packaged_template_and_configuration(tmp_path):
    script = _installed_script(tmp_path)
    assert "BASELINE_ONLY = True" in script.read_text(encoding="utf-8")
    config = json.loads((script.parent / "script.config").read_text(encoding="utf-8"))
    assert config["project_type"] == "Demo"
    assert "fictional" in config["list_description"].lower()


def test_reruns_rebuild_without_duplicate_records(tmp_path):
    script = _installed_script(tmp_path)
    db = _run(script)
    assert _people(db) == (
        [(1, "Alex Morgan"), (2, "Casey Brooks"), (3, "Jordan Lee")],
        [(1, "DEMO-001"), (2, "DEMO-002"), (3, "DEMO-003")],
    )
    original_bytes = db.read_bytes()
    _run(script)
    assert db.read_bytes() == original_bytes
    assert len(_people(db)[0]) == 3


def test_comparison_acceptance_history_and_toggle(tmp_path):
    script = _installed_script(tmp_path)
    workspace = script.parent.parent
    canonical_dir = tmp_path / "Canonical Outputs"
    canonical = canonical_dir / "900003.sqlite"
    db = _run(script)
    assert compare_feature("900003", workspace, canonical_dir).state == "NO CANONICAL"
    safe_replace_from_candidate(db, canonical)
    assert compare_feature("900003", workspace, canonical_dir).state == "SAME"

    contents = script.read_text(encoding="utf-8")
    script.write_text(contents.replace("BASELINE_ONLY = True", "BASELINE_ONLY = False", 1),
                      encoding="utf-8")
    _run(script)
    assert len(_people(db)[0]) == 4
    difference = compare_feature("900003", workspace, canonical_dir)
    assert difference.state == "DIFFERENT"
    assert difference.summary is not None
    assert "DEMO-004" in difference.summary.added_identifiers

    safe_replace_from_candidate(db, canonical)
    assert compare_feature("900003", workspace, canonical_dir).state == "SAME"
    snapshots = list((tmp_path / "Canonical Output History" / "900003").glob("*.sqlite"))
    assert len(snapshots) == 1
    assert len(_people(snapshots[0])[0]) == 3
    assert len(_people(canonical)[0]) == 4
