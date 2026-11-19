#!/usr/bin/env python3

from __future__ import annotations

from ..infrastructure.runtime_config import worker_runtime_settings, worker_workspace

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional


# ===================== CONFIGURATION =====================

DEFAULT_SCRIPT_RUNS_DIR = worker_workspace(worker_runtime_settings(__file__))

# =========================================================


@dataclass
class PackResult:
    feature: str              # e.g. "30234"
    status: str               # "success", "failure", "skipped"
    message: str              # short human-readable summary
    zip_created: bool         # True if feature_XXXX.zip was created successfully


def _find_latest_sqlite(feature_dir: Path, feature: str) -> Optional[Path]:
    """
    Determine which sqlite file to pack:
    1. feature_number.sqlite if present
    2. otherwise, newest *.sqlite
    """
    alias = feature_dir / f"{feature}.sqlite"
    if alias.exists():
        return alias

    candidates = list(feature_dir.glob("*.sqlite"))
    if not candidates:
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _pack_single_feature(
    feature: str,
    script_runs_dir: Path,
) -> PackResult:
    feature = str(feature).strip()
    folder_name = f"feature_{feature}"
    feature_dir = script_runs_dir / folder_name

    if not feature_dir.exists():
        return PackResult(
            feature=feature,
            status="skipped",
            message="feature directory not found",
            zip_created=False,
        )

    main_py = feature_dir / "main.py"
    config_file = feature_dir / "script.config"

    if not main_py.exists():
        return PackResult(
            feature=feature,
            status="failure",
            message="main.py not found",
            zip_created=False,
        )

    if not config_file.exists():
        return PackResult(
            feature=feature,
            status="failure",
            message="script.config not found",
            zip_created=False,
        )

    sqlite_src = _find_latest_sqlite(feature_dir, feature)
    if sqlite_src is None:
        return PackResult(
            feature=feature,
            status="failure",
            message="No sqlite file found to pack",
            zip_created=False,
        )

    # inner working directory
    inner_dir = feature_dir / folder_name
    zip_path = feature_dir / f"{folder_name}.zip"

    # Cleanup
    if inner_dir.exists():
        shutil.rmtree(inner_dir, ignore_errors=True)

    if zip_path.exists():
        try:
            zip_path.unlink()
        except Exception as e:
            return PackResult(
                feature=feature,
                status="failure",
                message=f"Failed to remove old zip: {e}",
                zip_created=False,
            )

    # Recreate folder
    try:
        inner_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return PackResult(
            feature=feature,
            status="failure",
            message=f"Failed to create inner directory: {e}",
            zip_created=False,
        )

    # Copy files
    try:
        shutil.copy2(main_py, inner_dir / main_py.name)
        shutil.copy2(config_file, inner_dir / config_file.name)
        shutil.copy2(sqlite_src, inner_dir / sqlite_src.name)
    except Exception as e:
        return PackResult(
            feature=feature,
            status="failure",
            message=f"Failed to copy files: {e}",
            zip_created=False,
        )

    # Make zip
    try:
        shutil.make_archive(
            str(inner_dir),
            "zip",
            root_dir=feature_dir,
            base_dir=inner_dir.name,
        )
    except Exception as e:
        return PackResult(
            feature=feature,
            status="failure",
            message=f"Failed to create zip: {e}",
            zip_created=False,
        )

    return PackResult(
        feature=feature,
        status="success",
        message=f"Packed feature into {folder_name}.zip",
        zip_created=True,
    )


def pack_features(
    features: Iterable[str],
    script_runs_dir: Path = DEFAULT_SCRIPT_RUNS_DIR,
    progress_callback: Optional[Callable[[PackResult], None]] = None,
) -> List[PackResult]:

    script_runs_dir = Path(script_runs_dir)
    features = [str(f).strip() for f in features if str(f).strip()]
    results: List[PackResult] = []

    for feature in features:
        result = _pack_single_feature(feature, script_runs_dir)
        results.append(result)

        if progress_callback:
            try:
                progress_callback(result)
            except Exception:
                pass

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Pack feature scripts into zip files.")
    parser.add_argument("features", nargs="+", help="Feature numbers to pack")
    parser.add_argument(
        "--script-runs-dir",
        type=str,
        default=str(DEFAULT_SCRIPT_RUNS_DIR),
        help="Root directory containing feature_XXXX folders.",
    )

    args = parser.parse_args()

    pack_features(
        features=args.features,
        script_runs_dir=Path(args.script_runs_dir),
    )
