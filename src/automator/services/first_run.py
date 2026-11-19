"""Safe first-run initialization for a writable Automator application storage root.

The root contains data/, logs/, and optionally features/. Never initialize or
write to the Python package installation directory. Existing files are never
modified, even if malformed: a user must explicitly repair damaged data.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..infrastructure.runtime_config import RuntimeSettings, worker_workspace
from ..infrastructure.canonical_paths import canonical_paths
from .example_feature import EXAMPLE_RECORD, install_first_run_example

class FirstRunError(RuntimeError):
    """A first-run directory or file could not be initialized safely."""


@dataclass(frozen=True)
class FirstRunResult:
    records_path: Path
    metadata_path: Path
    workspace_path: Path
    created: tuple[Path, ...]


def _create_json_exclusively(path: Path, content: object) -> bool:
    """Atomically claim an absent file, never truncating an existing file."""
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not path.is_file():
            raise FirstRunError(f"Expected a file but found another type: {path}")
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(content, out, indent=2, ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
    except Exception:
        # A failed first write must never masquerade as a valid initialized file.
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return True


def initialize_first_run(settings: RuntimeSettings) -> FirstRunResult:
    """Initialize only missing user data, metadata and a default feature workspace.

    This is safe to repeat. It never edits any preexisting file, user scripts,
    work history, or ClickUp state, and it does not require ClickUp or system Python.
    """
    root = Path(settings.data_root).expanduser().resolve()
    data_dir = root / "data"
    records = data_dir / "records.json"
    metadata = data_dir / "records_meta.json"
    workspace = worker_workspace(settings)
    created: list[Path] = []
    try:
        if not root.exists():
            root.mkdir(parents=True, exist_ok=True)
            created.append(root)
        if not data_dir.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
            created.append(data_dir)
        if not data_dir.is_dir():
            raise FirstRunError(f"Application data directory is not a directory: {data_dir}")

        if _create_json_exclusively(metadata, {
            "records_version": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "writer": "AutomatorFirstRun",
        }):
            created.append(metadata)

        if not workspace.exists():
            workspace.mkdir(parents=True, exist_ok=True)
            created.append(workspace)
        if not workspace.is_dir():
            raise FirstRunError(f"Feature workspace is not a directory: {workspace}")

        paths = canonical_paths(root, workspace)
        for directory in (paths.outputs, paths.history):
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                created.append(directory)
            if not directory.is_dir():
                raise FirstRunError(f"Canonical storage path is not a directory: {directory}")
        if not records.exists():
            example = install_first_run_example(workspace)
            initial_records = [dict(EXAMPLE_RECORD)] if example is not None else []
            if example is not None:
                created.append(example)
                created.extend((example / "main.py", example / "script.config"))
            if _create_json_exclusively(records, initial_records):
                created.append(records)
    except (OSError, ValueError, FirstRunError) as exc:
        raise FirstRunError(f"Cannot initialize Automator first-run storage: {exc}") from exc

    return FirstRunResult(records, metadata, workspace, tuple(created))
