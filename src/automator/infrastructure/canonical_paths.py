"""Canonical SQLite storage locations for installed and legacy Automator.

This module does no filesystem I/O. The existing development installation's
canonical directory is preserved when its workspace is its application root's
parent; installed applications store canonicals in user data, never beside EXE.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CanonicalPaths:
    outputs: Path
    history: Path


def canonical_paths(data_root: Path, workspace: Path) -> CanonicalPaths:
    app_root = Path(data_root).expanduser().resolve()
    feature_workspace = Path(workspace).expanduser().resolve()
    # Existing developer layout: D:/.../Script Runs/Automator (app root)
    # and D:/.../Script Runs (workspace). Preserve its canonical directory.
    base = feature_workspace if feature_workspace == app_root.parent else app_root
    return CanonicalPaths(base / 'Canonical Outputs', base / 'Canonical Output History')
