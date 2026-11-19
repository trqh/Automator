#!/usr/bin/env python3
from __future__ import annotations

from ..infrastructure.runtime_config import worker_runtime_settings, worker_workspace

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

# Root directory where all feature_XXXX folders live (same default pattern as other workers)
DEFAULT_SCRIPT_RUNS_DIR = worker_workspace(worker_runtime_settings(__file__))


@dataclass
class CopyZipResult:
    feature: str              # e.g. "30234"
    status: str               # "success", "failure", "skipped"
    message: str              # short human-readable summary
    src_zip: Optional[str]    # source zip path (string) if found
    dest_zip: Optional[str]   # destination zip path (string) if copied


def _copy_single_zip(feature: str, script_runs_dir: Path, dest_dir: Path) -> CopyZipResult:
    feature = str(feature).strip()
    folder_name = f"feature_{feature}"
    feature_dir = script_runs_dir / folder_name
    zip_path = feature_dir / f"{folder_name}.zip"

    if not feature_dir.exists():
        return CopyZipResult(
            feature=feature,
            status="skipped",
            message="feature directory not found",
            src_zip=None,
            dest_zip=None,
        )

    if not zip_path.exists():
        return CopyZipResult(
            feature=feature,
            status="skipped",
            message="zip not found (run Pack selected first)",
            src_zip=str(zip_path),
            dest_zip=None,
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_zip = dest_dir / zip_path.name

    try:
        shutil.copy2(zip_path, dest_zip)  # overwrite if exists
    except Exception as e:
        return CopyZipResult(
            feature=feature,
            status="failure",
            message=f"failed to copy zip: {e}",
            src_zip=str(zip_path),
            dest_zip=str(dest_zip),
        )

    return CopyZipResult(
        feature=feature,
        status="success",
        message=f"copied to {dest_zip}",
        src_zip=str(zip_path),
        dest_zip=str(dest_zip),
    )


def copy_packed_zips(
    features: Iterable[str],
    script_runs_dir: Path = DEFAULT_SCRIPT_RUNS_DIR,
    dest_dir: Optional[Path] = None,
    progress_callback: Optional[Callable[[CopyZipResult], None]] = None,
) -> List[CopyZipResult]:
    """
    Copies feature_XXXX.zip files (created by pack_features_worker) into a single directory.

    - features: iterable of feature numbers
    - script_runs_dir: root containing feature_XXXX folders
    - dest_dir: destination directory (created if missing)
    - progress_callback: optional callback called per result
    """
    script_runs_dir = Path(script_runs_dir)
    if dest_dir is None:
        raise ValueError("dest_dir must be provided")
    dest_dir = Path(dest_dir)

    features = [str(f).strip() for f in features if str(f).strip()]
    results: List[CopyZipResult] = []

    for feature in features:
        res = _copy_single_zip(feature, script_runs_dir, dest_dir)
        results.append(res)
        if progress_callback:
            try:
                progress_callback(res)
            except Exception:
                pass

    return results
