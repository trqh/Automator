#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sqlite3
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional
from ..infrastructure.storage_paths import legacy_storage
from ..infrastructure.runtime_config import worker_runtime_settings, worker_workspace

# ===================== CONFIGURATION =====================

# Root directory where all feature_XXXX folders live
RUNTIME_SETTINGS = worker_runtime_settings(__file__)
APP_ROOT = RUNTIME_SETTINGS.data_root
DEFAULT_SCRIPT_RUNS_DIR = worker_workspace(RUNTIME_SETTINGS)
STORAGE = legacy_storage(APP_ROOT)
DEFAULT_OUTPUTS_ROOT = APP_ROOT / "Outputs"
DEFAULT_BATCH_METADATA = STORAGE.path("batch_metadata")
BIG_FILE_THRESHOLD = 10000
_METADATA_LOCK = threading.Lock()

# =========================================================


@dataclass
class BatchResult:
    feature: str              # e.g. "30234"
    status: str               # "success", "failure", "skipped"
    message: str              # short human-readable summary
    dest_path: Optional[str]  # final path of copied sqlite, or None
    category: str             # "L&E", "Sanctions", "Whitelists", "Controversial Weapons", "Uncategorized"
    person_count: Optional[int] = None
    output_class: Optional[str] = None
    output_folder: Optional[str] = None
    metadata_error: Optional[str] = None


def _find_sqlite_for_feature(feature_dir: Path, feature: str) -> Optional[Path]:
    """
    Decide which sqlite file to use for batching:
    1. feature_number.sqlite (if present)
    2. Otherwise, the most recent *.sqlite in the directory
    """
    alias = feature_dir / f"{feature}.sqlite"
    if alias.exists():
        return alias

    candidates = list(feature_dir.glob("*.sqlite"))
    if not candidates:
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _count_persons(sqlite_path: Path) -> int:
    """Count persons without opening the candidate for writes."""
    uri = sqlite_path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=10) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? COLLATE NOCASE",
                ("person",),
            ).fetchone()
            if exists is None:
                raise ValueError("person table is missing")
            row = connection.execute("SELECT COUNT(*) FROM person").fetchone()
            if row is None or row[0] is None:
                raise ValueError("person count query returned no result")
            return int(row[0])
    except (sqlite3.Error, OSError) as exc:
        raise ValueError("could not read person table: %s" % exc) from exc


def _normalize_rationale(value: str) -> str:
    """
    Normalize rationale values to make matching more lax:
    - lowercase
    - strip leading/trailing whitespace
    - collapse internal whitespace
    """
    v = value.lower().strip()
    if not v:
        return ""
    # Replace various separators with spaces
    for sep in ["_", "-", "/"]:
        v = v.replace(sep, " ")
    # Collapse multiple spaces
    v = " ".join(v.split())
    return v


def _classify_category_from_config(config_path: Path) -> str:
    """
    Read script.config (JSON) and derive category from rationale_type.

    Lax mapping examples:
        if v in {"l&e", "l & e", "l and e", "le", "l e"} -> "L&E"
        if v in {"whitelist", "whitelists"} -> "Whitelists"
        if v in {"sanction", "sanctions"} -> "Sanctions"
        plus a more tolerant normalization.

    Includes "Controversial Weapons" as well, with lax matching.
    """
    try:
        with config_path.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return "Uncategorized"

    raw = str(cfg.get("rationale_type", "")).strip()
    if not raw:
        return "Uncategorized"

    v = _normalize_rationale(raw)           # e.g. "l e", "controversial weapons", "sanctions retail"
    v_compact = v.replace(" ", "")          # e.g. "le", "controversialweapons", "sanctionsretail"

    # --- L&E family ---
    le_synonyms = {
        "l&e",
        "l & e",
        "l and e",
        "le",
        "l e",
    }
    if raw.lower().strip() in le_synonyms or v in le_synonyms or v_compact in {"le"}:
        return "L&E"
    # also accept strings that start with these tokens
    if v.startswith("l&e") or v.startswith("l and e") or v.startswith("le "):
        return "L&E"

    # --- Whitelists ---
    wl_synonyms = {
        "whitelist",
        "whitelists",
        "white list",
        "white lists",
    }
    if raw.lower().strip() in wl_synonyms or v in wl_synonyms:
        return "Whitelists"
    if "whitelist" in v or "white list" in v:
        return "Whitelists"

    # --- Sanctions ---
    sanc_synonyms = {
        "sanction",
        "sanctions",
    }
    if raw.lower().strip() in sanc_synonyms or v in sanc_synonyms:
        return "Sanctions"
    if "sanction" in v:
        return "Sanctions"

    # --- Controversial Weapons ---
    # Allow "controversial weapons", "controversial weapon", "cw", etc.
    cw_synonyms = {
        "controversial weapons",
        "controversial weapon",
        "cw",
    }
    if raw.lower().strip() in cw_synonyms or v in cw_synonyms:
        return "Controversial Weapons"
    if "controversial" in v and "weapon" in v:
        return "Controversial Weapons"
    if v_compact.startswith("cw"):
        return "Controversial Weapons"

    return "Uncategorized"


def _get_category_dir(outputs_root: Path, category: str) -> Path:
    """
    Map category name to directory under Outputs.
    "Uncategorized" -> Outputs root
    Others -> Outputs/<category>
    """
    if category == "L&E":
        return outputs_root / "L&E"
    if category == "Sanctions":
        return outputs_root / "Sanctions"
    if category == "Whitelists":
        return outputs_root / "Whitelists"
    if category == "Controversial Weapons":
        return outputs_root / "Controversial Weapons"
    return outputs_root / category if category and category != "Uncategorized" else outputs_root


def _ensure_all_category_dirs(outputs_root: Path) -> None:
    """
    Ensure Outputs root and all category subdirectories exist,
    even if they are currently empty.
    """
    outputs_root.mkdir(parents=True, exist_ok=True)
    for sub in ["L&E", "Sanctions", "Whitelists", "Controversial Weapons", "Big Files"]:
        (outputs_root / sub).mkdir(parents=True, exist_ok=True)


def _batch_single_feature(
    feature: str,
    script_runs_dir: Path,
    outputs_root: Path,
) -> BatchResult:
    feature = str(feature).strip()
    if not feature.isascii() or not feature.isdigit():
        return BatchResult(feature, "failure", "invalid feature number", None, "Uncategorized")
    folder_name = f"feature_{feature}"
    feature_dir = script_runs_dir / folder_name

    if not feature_dir.exists():
        return BatchResult(
            feature=feature,
            status="skipped",
            message="feature directory not found",
            dest_path=None,
            category="Uncategorized",
        )

    config_file = feature_dir / "script.config"
    if not config_file.exists():
        return BatchResult(
            feature=feature,
            status="failure",
            message="script.config not found",
            dest_path=None,
            category="Uncategorized",
        )

    sqlite_src = _find_sqlite_for_feature(feature_dir, feature)
    if sqlite_src is None:
        return BatchResult(
            feature=feature,
            status="failure",
            message="No sqlite file found to batch",
            dest_path=None,
            category="Uncategorized",
        )

    # Determine category from config (using lax matching)
    category = _classify_category_from_config(config_file)

    try:
        person_count = _count_persons(sqlite_src)
    except Exception as e:
        return BatchResult(
            feature=feature, status="failure", message="Person count unavailable; routing requires review: %s" % e,
            dest_path=None, category=category, output_class="REVIEW",
        )

    output_class = "Big Files" if person_count >= BIG_FILE_THRESHOLD else "Regular"
    # Ensure output directories exist only after the input passes inspection.
    _ensure_all_category_dirs(outputs_root)
    category_dir = outputs_root / "Big Files" if output_class == "Big Files" else _get_category_dir(outputs_root, category)

    dest_path = category_dir / f"{feature}.sqlite"

    try:
        shutil.copy2(sqlite_src, dest_path)
    except Exception as e:
        return BatchResult(
            feature=feature,
            status="failure",
            message=f"Failed to copy sqlite: {e}",
            dest_path=None,
            category=category,
            person_count=person_count,
            output_class=output_class,
            output_folder=str(category_dir),
        )

    return BatchResult(
        feature=feature,
        status="success",
        message=f"Copied {sqlite_src.name} to {dest_path}",
        dest_path=str(dest_path),
        category=category,
        person_count=person_count,
        output_class=output_class,
        output_folder=str(category_dir),
    )


def _persist_batch_results(results, metadata_path: Path) -> None:
    successful = [result for result in results if result.status == "success"]
    if not successful:
        return
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    with _METADATA_LOCK:
        try:
            previous = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
            if not isinstance(previous, dict):
                previous = {}
        except Exception:
            previous = {}
        now = datetime.now(timezone.utc).isoformat()
        for result in successful:
            previous[result.feature] = {
                "feature_number": result.feature,
                "project_type": result.category,
                "person_count": result.person_count,
                "output_class": result.output_class,
                "output_folder": result.output_folder,
                "output_path": result.dest_path,
                "batched_at": now,
            }
        handle, temporary = tempfile.mkstemp(prefix=metadata_path.name + ".", suffix=".tmp", dir=str(metadata_path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(previous, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, metadata_path)
        finally:
            if os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def batch_outputs(
    features: Iterable[str],
    script_runs_dir: Path = DEFAULT_SCRIPT_RUNS_DIR,
    outputs_root: Path = DEFAULT_OUTPUTS_ROOT,
    progress_callback: Optional[Callable[[BatchResult], None]] = None,
    metadata_path: Path = DEFAULT_BATCH_METADATA,
) -> List[BatchResult]:
    """
    Batch outputs for selected features.

    For each feature:
    - go into Script Runs/feature_XXXX
    - choose feature_number.sqlite or latest .sqlite
    - read script.config to determine rationale_type (lax matching)
    - copy sqlite into Outputs/<category>:
        L&E, Sanctions, Whitelists, Controversial Weapons, or Outputs root.

    - features: iterable of feature numbers
    - script_runs_dir: root directory containing feature_XXXX
    - outputs_root: where 'Outputs' lives
    - progress_callback: optional callback called with each BatchResult

    Returns:
        List[BatchResult] for all requested features.
    """
    script_runs_dir = Path(script_runs_dir)
    outputs_root = Path(outputs_root)

    # Ensure root + subdirectories always exist
    _ensure_all_category_dirs(outputs_root)

    features = [str(f).strip() for f in features if str(f).strip()]
    results: List[BatchResult] = []

    if not features:
        return results

    for feature in features:
        result = _batch_single_feature(feature, script_runs_dir, outputs_root)
        results.append(result)

    try:
        _persist_batch_results(results, Path(metadata_path))
    except Exception as exc:
        for result in results:
            if result.status == "success":
                result.metadata_error = str(exc)
                result.message += "; batch metadata could not be saved: %s" % exc

    if progress_callback:
        for result in results:
            try:
                progress_callback(result)
            except Exception:
                # Don't let GUI/callback errors break the worker.
                pass

    return results


if __name__ == "__main__":
    # Simple CLI usage example:
    #   python batch_outputs_worker.py 30234 32404
    import argparse

    parser = argparse.ArgumentParser(description="Batch sqlite outputs for features.")
    parser.add_argument("features", nargs="+", help="Feature numbers to batch outputs for")
    parser.add_argument(
        "--script-runs-dir",
        type=str,
        default=str(DEFAULT_SCRIPT_RUNS_DIR),
        help="Root directory containing feature_XXXX folders.",
    )
    parser.add_argument(
        "--outputs-root",
        type=str,
        default=str(DEFAULT_OUTPUTS_ROOT),
        help="Root directory where 'Outputs' will be created.",
    )

    args = parser.parse_args()

    batch_outputs(
        features=args.features,
        script_runs_dir=Path(args.script_runs_dir),
        outputs_root=Path(args.outputs_root),
    )
