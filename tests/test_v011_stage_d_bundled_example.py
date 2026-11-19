"""Stage D: bundled interpreter only for allowlisted feature 900003 bytes."""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from automator.infrastructure.runtime_config import RuntimeSettings
from automator.services.trusted_example import is_trusted_example
from automator.workers.run_features_worker import run_features


def install(workspace: Path) -> Path:
    directory = workspace / "feature_900003"
    directory.mkdir(parents=True)
    source = files("automator.demo_scripts.feature_900003")
    for name in ("main.py", "script.config"):
        (directory / name).write_bytes(source.joinpath(name).read_bytes())
    return directory / "main.py"


def run(workspace: Path, root: Path, *, features=("900003",), python=None):
    return run_features(
        features=features, script_runs_dir=workspace, logs_dir=root / "logs",
        runtime_settings=RuntimeSettings(data_root=root), max_workers=1,
        frozen=True, bundled_python=python,
    )


def test_allowlist_accepts_only_shipped_script_or_toggle(tmp_path):
    workspace = tmp_path / "features"
    script = install(workspace)
    assert is_trusted_example("900003", workspace)
    assert not is_trusted_example("900004", workspace)
    script.write_bytes(script.read_bytes().replace(b"BASELINE_ONLY = True", b"BASELINE_ONLY = False", 1))
    assert is_trusted_example("900003", workspace)
    script.write_bytes(script.read_bytes() + b"\nprint('other code')\n")
    assert not is_trusted_example("900003", workspace)


def test_symlink_and_missing_script_not_allowlisted(tmp_path, monkeypatch):
    workspace = tmp_path / "features"
    assert not is_trusted_example("900003", workspace)
    script = install(workspace)
    assert is_trusted_example("900003", workspace)
    script.unlink()
    assert not is_trusted_example("900003", workspace)
    external = tmp_path / "external.py"
    external.write_bytes(files("automator.demo_scripts.feature_900003").joinpath("main.py").read_bytes())
    try:
        script.symlink_to(external)
    except (OSError, NotImplementedError):
        # Windows may restrict actual symlink creation. Exercise the same
        # rejection branch with byte-identical, real regular-file content.
        # This verifies policy without granting elevated OS permissions.
        script.write_bytes(external.read_bytes())
        original_is_symlink = Path.is_symlink

        def simulated_symlink(path):
            return path == script or original_is_symlink(path)

        with monkeypatch.context() as isolated:
            isolated.setattr(Path, "is_symlink", simulated_symlink)
            assert not is_trusted_example("900003", workspace)
        assert is_trusted_example("900003", workspace)
    else:
        assert not is_trusted_example("900003", workspace)


def test_frozen_normal_worker_runs_both_exact_variants_without_external_python(tmp_path):
    workspace = tmp_path / "features"
    script = install(workspace)
    for enabled, expected in ((True, 3), (False, 4)):
        if not enabled:
            script.write_bytes(script.read_bytes().replace(b"BASELINE_ONLY = True", b"BASELINE_ONLY = False", 1))
        results = run(workspace, tmp_path, python=Path(sys.executable))
        assert len(results) == 1 and results[0].status == "success", results
        with closing(sqlite3.connect(script.parent / "900003.sqlite")) as conn:
            assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == expected


def test_frozen_edited_example_requires_external_interpreter(tmp_path):
    workspace = tmp_path / "features"
    script = install(workspace)
    script.write_bytes(script.read_bytes() + b"\n# user modification\n")
    with pytest.raises(RuntimeError, match="external Python interpreter"):
        run(workspace, tmp_path, python=Path(sys.executable))


def test_frozen_unrelated_feature_requires_external_interpreter(tmp_path):
    workspace = tmp_path / "features"
    install(workspace)
    other = workspace / "feature_900004"
    other.mkdir()
    shutil.copy2(workspace / "feature_900003" / "main.py", other / "main.py")
    with pytest.raises(RuntimeError, match="external Python interpreter"):
        run(workspace, tmp_path, features=("900004",), python=Path(sys.executable))
