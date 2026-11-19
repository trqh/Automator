#!/usr/bin/env python3
from __future__ import annotations

from ..infrastructure.runtime_config import worker_runtime_settings, worker_workspace

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional


DEFAULT_SCRIPT_RUNS_DIR = worker_workspace(worker_runtime_settings(__file__))


@dataclass
class CleanDirectoryResult:
    feature: str
    status: str              # "success", "failure", "skipped"
    message: str
    kept: List[str]
    removed: List[str]


def _pick_sqlite_to_keep(feature_dir: Path, feature: str) -> Optional[Path]:
    preferred = feature_dir / f"{feature}.sqlite"
    if preferred.exists():
        return preferred

    candidates = list(feature_dir.glob("*.sqlite"))
    if not candidates:
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _clean_single_feature(feature: str, script_runs_dir: Path) -> CleanDirectoryResult:
    feature = str(feature).strip()
    folder_name = f"feature_{feature}"
    feature_dir = script_runs_dir / folder_name

    if not feature_dir.exists():
        return CleanDirectoryResult(
            feature=feature,
            status="skipped",
            message="feature directory not found",
            kept=[],
            removed=[],
        )

    if not feature_dir.is_dir():
        return CleanDirectoryResult(
            feature=feature,
            status="skipped",
            message="feature path is not a directory",
            kept=[],
            removed=[],
        )

    main_py = feature_dir / "main.py"
    script_config = feature_dir / "script.config"
    sqlite_to_keep = _pick_sqlite_to_keep(feature_dir, feature)
    packed_inner_dir = feature_dir / folder_name

    keep_paths = set()
    if main_py.exists():
        keep_paths.add(main_py.resolve())
    if script_config.exists():
        keep_paths.add(script_config.resolve())
    if sqlite_to_keep is not None and sqlite_to_keep.exists():
        keep_paths.add(sqlite_to_keep.resolve())
    if packed_inner_dir.exists() and packed_inner_dir.is_dir():
        keep_paths.add(packed_inner_dir.resolve())

    kept: List[str] = []
    removed: List[str] = []

    try:
        for child in feature_dir.iterdir():
            child_resolved = child.resolve()
            if child_resolved in keep_paths:
                kept.append(child.name)
                continue

            _remove_path(child)
            removed.append(child.name)

        if main_py.exists() and main_py.name not in kept:
            kept.append(main_py.name)
        if script_config.exists() and script_config.name not in kept:
            kept.append(script_config.name)
        if sqlite_to_keep is not None and sqlite_to_keep.exists() and sqlite_to_keep.name not in kept:
            kept.append(sqlite_to_keep.name)
        if packed_inner_dir.exists() and packed_inner_dir.is_dir() and packed_inner_dir.name not in kept:
            kept.append(packed_inner_dir.name)

        kept.sort()
        removed.sort()

        return CleanDirectoryResult(
            feature=feature,
            status="success",
            message=f"kept {len(kept)} item(s), removed {len(removed)} item(s)",
            kept=kept,
            removed=removed,
        )
    except Exception as e:
        kept.sort()
        removed.sort()
        return CleanDirectoryResult(
            feature=feature,
            status="failure",
            message=f"cleanup failed: {e}",
            kept=kept,
            removed=removed,
        )


def clean_feature_directories(
    features: Iterable[str],
    script_runs_dir: Path = DEFAULT_SCRIPT_RUNS_DIR,
    progress_callback: Optional[Callable[[CleanDirectoryResult], None]] = None,
) -> List[CleanDirectoryResult]:
    script_runs_dir = Path(script_runs_dir)
    features = [str(f).strip() for f in features if str(f).strip()]
    results: List[CleanDirectoryResult] = []

    for feature in features:
        res = _clean_single_feature(feature, script_runs_dir)
        results.append(res)

        if progress_callback:
            try:
                progress_callback(res)
            except Exception as e:
                pass

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Clean feature directories by keeping main.py, script.config, the preferred/latest sqlite, and the inner packed feature_XXXX directory."
    )
    parser.add_argument("features", nargs="+", help="Feature numbers to clean")
    parser.add_argument(
        "--script-runs-dir",
        default=str(DEFAULT_SCRIPT_RUNS_DIR),
        help="Root directory containing feature_XXXX folders",
    )
    args = parser.parse_args()

    results = clean_feature_directories(
        features=args.features,
        script_runs_dir=Path(args.script_runs_dir),
    )

    failed = 0
    for res in results:
        print(f"[{res.status.upper()}] feature_{res.feature}: {res.message}")
        if res.kept:
            print("  kept: " + ", ".join(res.kept))
        if res.removed:
            print("  removed: " + ", ".join(res.removed))
        if res.status == "failure":
            failed += 1

    raise SystemExit(1 if failed else 0)
