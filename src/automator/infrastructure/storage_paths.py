"""Centralized storage contract for Feature Manager.

Batch 2G-B: legacy is the active layout until a verified migration activates
organized layout. Importing this module never creates or moves files.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Layout = Literal["legacy", "organized"]

# Files whose locations are owned by this module. Canonical SQLite output
# history and user feature outputs are intentionally outside this map.
_RELATIVE_PATHS: dict[str, tuple[str, str]] = {
    "work_ledger": ("logs/work_tracking.sqlite3", "logs/meta/work_tracking.sqlite3"),
    "batch_metadata": ("logs/batch_metadata.json", "logs/meta/batch_metadata.json"),
    "clickup_state": ("logs/clickup_sync_state.json", "logs/meta/clickup_sync_state.json"),
    "clickup_daily_state": ("logs/clickup_daily_sync_state.json", "logs/meta/clickup_daily_sync_state.json"),
    "dataops_state": ("logs/dataops_sync_state.json", "logs/meta/dataops_sync_state.json"),
    "clickup_last_run": ("logs/clickup_task_id_sync_last_run.json", "logs/reports/sync/clickup_task_id_sync_last_run.json"),
    "dataops_last_run": ("logs/sync_last_run.json", "logs/reports/sync/sync_last_run.json"),
    "reconciliation": ("logs/workday_reconciliation.json", "logs/reports/reconciliation/workday_reconciliation.json"),
    "gui_actions": ("logs/gui_actions.jsonl", "logs/runs/gui/gui_actions.jsonl"),
    "structured_events": ("logs/structured_events.jsonl", "logs/runs/events/structured_events.jsonl"),
    "run_history": ("run_history.jsonl", "logs/runs/features/run_history.jsonl"),
    "sessions": ("logs/sessions", "logs/runs/sessions"),
    "feature_runs": ("logs", "logs/runs/features"),
    "sync_errors": ("logs", "logs/reports/sync"),
    "temp": ("logs", "logs/temp"),
}

_ORGANIZED_FOLDERS = (
    "logs/meta", "logs/reports/sync", "logs/reports/validation",
    "logs/reports/comparison", "logs/reports/batching",
    "logs/reports/reconciliation", "logs/runs/gui",
    "logs/runs/features", "logs/runs/sessions", "logs/runs/events",
    "logs/archive/reports", "logs/archive/runs",
    "logs/archive/notes", "logs/archive/metadata-snapshots",
    "logs/temp",
)


@dataclass(frozen=True)
class StoragePaths:
    """Resolve paths without I/O. `root` is the Automator working directory."""

    root: Path
    layout: Layout = "legacy"

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        if self.layout not in ("legacy", "organized"):
            raise ValueError(f"Unsupported storage layout: {self.layout!r}")

    @property
    def data(self) -> Path:
        """Persistent application records and preferences."""
        return self.root / "data"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def run_history_archive_dir(self) -> Path:
        """Directory for rotated run-history files."""
        if self.layout == "legacy":
            return self.path("run_history").parent
        return self.logs / "archive" / "runs"

    def path(self, key: str) -> Path:
        if key not in _RELATIVE_PATHS:
            raise KeyError(f"Unknown storage path key: {key}")
        legacy, organized = _RELATIVE_PATHS[key]
        return self.root / (legacy if self.layout == "legacy" else organized)

    def ensure_directories(self) -> None:
        """Create folder structure, never relocate, modify, or delete files."""
        self.logs.mkdir(parents=True, exist_ok=True)
        if self.layout == "organized":
            for folder in _ORGANIZED_FOLDERS:
                (self.root / folder).mkdir(parents=True, exist_ok=True)

    def migration_pairs(self) -> tuple[tuple[Path, Path], ...]:
        """Candidate file/dir moves for later audited migration; no I/O."""
        legacy = StoragePaths(self.root, "legacy")
        organized = StoragePaths(self.root, "organized")
        # Directories containing unrelated files must never be treated as
        # one-file migration sources. Sessions are migrated as a complete tree.
        movable_keys = set(_RELATIVE_PATHS) - {"feature_runs", "sync_errors", "temp"}
        return tuple(
            (legacy.path(key), organized.path(key))
            for key in _RELATIVE_PATHS
            if key in movable_keys and legacy.path(key) != organized.path(key)
        )



def legacy_storage(app_root: Path) -> StoragePaths:
    """Resolve the active layout; preserve legacy behavior by default."""
    root = Path(app_root).resolve()
    marker = root / "storage_layout.json"

    if not marker.exists():
        return StoragePaths(root, "legacy")

    import json

    config = json.loads(marker.read_text(encoding="utf-8"))
    layout = config.get("layout")

    if layout not in ("legacy", "organized"):
        raise ValueError(f"Invalid storage layout: {layout!r}")

    return StoragePaths(root, layout)
