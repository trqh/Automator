"""Isolated, offline demonstration runner for Automator's packaged GUI.

Runs the trusted demo through the feature worker; never touches real feature
records or the configured user workspace. An installed frozen release must ship
an independent Python interpreter in its runtime directory.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from ..infrastructure.runtime_config import RuntimeSettings, RuntimeSettingsError
from ..workers.run_features_worker import run_features


DEMO_ID = "900001"


@dataclass(frozen=True)
class DemoOutcome:
    succeeded: bool
    message: str
    run_directory: Path
    output_database: Path
    interpreter: Path


def bundled_demo_python(*, frozen: bool | None = None, executable: Path | None = None,
                        platform: str | None = None) -> Path:
    """Discover the separately shipped Python runtime, not the frozen GUI exe.

    AUTOMATOR_BUNDLED_PYTHON is primarily useful for explicit packaging layouts
    and controlled testing. Release packaging must supply and verify the runtime.
    """
    overridden = os.environ.get("AUTOMATOR_BUNDLED_PYTHON", "").strip()
    if overridden:
        return Path(overridden).expanduser().resolve()
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    active_executable = Path(sys.executable if executable is None else executable).resolve()
    if not is_frozen:
        return active_executable
    root = active_executable.parent
    os_name = sys.platform if platform is None else platform
    candidates = (
        [root / "runtime" / "python.exe", root / "python" / "python.exe"]
        if os_name == "win32" else
        [root / "runtime" / "bin" / "python3", root / "runtime" / "bin" / "python"]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # A nonexistent location yields a clear error through the runtime resolver.
    return candidates[0]


def run_offline_demo(settings: RuntimeSettings, *,
                     frozen: bool | None = None,
                     bundled_python: Path | None = None) -> DemoOutcome:
    """Run a bundled SQLite demo in a newly created, isolated user-data folder.

    No ClickUp, user features, records.json, or real workspace is accessed.
    All outputs are retained under data_root/demo_runs for inspection.
    """
    data_root = Path(settings.data_root).resolve()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = data_root / "demo_runs" / f"{stamp}_{uuid4().hex[:8]}"
    workspace = run_dir / "workspace"
    feature_dir = workspace / f"feature_{DEMO_ID}"
    feature_dir.mkdir(parents=True, exist_ok=False)
    resource_package = __package__.rsplit(".", 1)[0] + ".demo_scripts.feature_900001"
    resource_root = files(resource_package)
    for name in ("main.py", "script.config"):
        (feature_dir / name).write_bytes(resource_root.joinpath(name).read_bytes())

    interpreter = Path(bundled_python) if bundled_python is not None else bundled_demo_python(frozen=frozen)
    results = run_features(
        features=[DEMO_ID],
        script_runs_dir=workspace,
        logs_dir=run_dir / "logs",
        history_path=run_dir / "run_history.jsonl",
        max_workers=1,
        runtime_settings=settings,
        demo_features=[DEMO_ID],
        bundled_python=interpreter,
        frozen=frozen,
    )
    output = feature_dir / f"{DEMO_ID}.sqlite"
    result = results[0] if results else None
    if result is None or result.status != "success":
        detail = str(getattr(result, "message", "No result returned"))
        return DemoOutcome(False, detail, run_dir, output, interpreter)
    if not output.is_file():
        return DemoOutcome(False, "Demo ran but its SQLite output is missing", run_dir, output, interpreter)
    try:
        with sqlite3.connect(f"file:{output.as_posix()}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT label FROM demo LIMIT 1").fetchone()
        if row is None or row[0] != "Automator demo completed":
            return DemoOutcome(False, "Demo SQLite validation did not match", run_dir, output, interpreter)
    except sqlite3.Error as exc:
        return DemoOutcome(False, f"Demo SQLite validation failed: {exc}", run_dir, output, interpreter)
    return DemoOutcome(True, "Bundled SQLite demonstration completed successfully", run_dir, output, interpreter)
