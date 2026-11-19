#!/usr/bin/env python3
from __future__ import annotations
import os
import re
import time
import json
import shutil
import threading
import sys
from queue import Queue, Empty
from dataclasses import dataclass
from datetime import datetime, timedelta, date, time as datetime_time, timezone
from dateutil.relativedelta import relativedelta
from pathlib import Path
from typing import Dict, List, Optional
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import psutil

from ..workers.run_features_worker import run_features, RunResult, cancel_active_processes
from ..workers.pack_features_worker import pack_features, PackResult
from ..workers.batch_outputs_worker import batch_outputs, BatchResult
from ..workers.copy_packed_zips_worker import copy_packed_zips, CopyZipResult
from ..workers.clean_feature_directories_worker import clean_feature_directories, CleanDirectoryResult
from ..workers.feature_setup_worker import (
    FeatureSetupError,
    SetupInspection,
    initialize_features,
    inspect_feature_setups,
)
from ..workers.sqlite_compare_worker import (
    ComparisonResult,
    SQLiteDifferenceReport,
    compare_features,
    inspect_sqlite_differences,
    safe_replace_from_candidate,
    sha256_file,
)
from ..integrations.clickup.clickup_client import ClickUpClient, ClickUpError
from ..integrations.clickup.clickup_state import ClickUpStateStore
from ..integrations.clickup.clickup_workflow import (
    build_workflow_item,
    canonical_approval_is_current,
    invalidate_after_successful_run,
    mark_canonical_approved,
    mark_canonical_review_required,
    sync_clickup_features,
)
from .gui_columns import (
    default_column_visibility,
    display_column_id,
    display_column_name,
    normalize_column_visibility,
    visible_columns,
)
from ..services.work_ledger import (
    WorkLedger,
    WorkInterval,
    LedgerError,
    ValidationError as WorkLedgerValidationError,
    to_unix_milliseconds,
)
from ..infrastructure.storage_paths import legacy_storage
from ..services.first_run import FirstRunError, initialize_first_run
from ..infrastructure.runtime_config import (
    worker_runtime_settings,
    worker_workspace,
)
from ..infrastructure.bundled_runtime import helper_command
from ..infrastructure.runtime_config import RuntimeSettingsError

from dataclasses import replace

from ..services.demo_runner import run_offline_demo

from ..infrastructure.runtime_config import (
    RuntimeSettingsError,
    probe_script_python,
    save_runtime_settings,
)

from ..infrastructure.canonical_paths import canonical_paths
from ..services.comparison_demo import prepare_comparison_demo, accept_comparison_demo

# ===================== PATH HELPERS =====================

def get_app_root() -> Path:
    return RUNTIME_SETTINGS.data_root


RUNTIME_SETTINGS = worker_runtime_settings(__file__)
APP_ROOT = get_app_root()
STORAGE = legacy_storage(APP_ROOT)

SYNC_CLICKUP_TASK_IDS_MODULE = (
    __package__.rsplit(".", 1)[0]
    + ".integrations.clickup.sync_clickup_task_ids"
)
DATA_DIR = STORAGE.data
RECORDS_JSON = DATA_DIR / "records.json"
GUI_STATE_JSON = DATA_DIR / "gui_state.json"
RUN_HISTORY_JSONL = STORAGE.path("run_history")
DEFAULT_SCRIPT_RUNS_DIR = worker_workspace(RUNTIME_SETTINGS)
RECORDS_LOCK = DATA_DIR / "records.json.lock"
RECORDS_META_JSON = DATA_DIR / "records_meta.json"
WORK_LEDGER_DB = STORAGE.path("work_ledger")
BATCH_METADATA_JSON = STORAGE.path("batch_metadata")

RUN_HISTORY_MAX_MB = 25          # rotate if run_history.jsonl exceeds this size
RUN_HISTORY_KEEP_LINES = 50000   # keep last N lines in the active file after rotation
GUI_ACTIONS_MAX_MB = 10          # prune gui_actions.jsonl if it exceeds this size
GUI_ACTIONS_KEEP_LINES = 20000   # keep last N lines after prune
SYNC_TRANSCRIPT_KEEP_FILES = 30  # keep only the most recent N sync transcript files
RECORDS_BACKUP_KEEP = 5          # keep five previous records.json generations

SESSIONS_ROOT = STORAGE.path("sessions")

STRUCTURED_EVENTS_JSONL = STORAGE.path("structured_events")
CORRELATION_ID_ENV_VAR = "FEATURE_MANAGER_CORRELATION_ID"
SESSION_ID_ENV_VAR = "FEATURE_MANAGER_SESSION_ID"

FREQUENCY_DELTAS = {
    "daily": relativedelta(days=1),
    "weekly": relativedelta(weeks=1),
    "monthly": relativedelta(months=1),
    "quarterly": relativedelta(months=3),
    "biannually": relativedelta(months=6),
    "annually": relativedelta(years=1),
}

STATUS_OPTIONS = [
    "To be analyzed",
    "Analyzed",
    "Reschedule",
    "Rejected",
    "Resolved",
    "Prepare for upload",
    "In Progress",
    "Scheduled",
    "Imported"
]

ALLOWED_FREQUENCIES = set(FREQUENCY_DELTAS.keys())
ALLOWED_LAST_RUN_STATUSES = {"", "success", "failure", "timeout", "skipped"}

# Map normalized workflow status -> canonical display from STATUS_OPTIONS
_CANON_STATUS = {s.strip().lower().replace(" ", "_"): s for s in STATUS_OPTIONS}
SENSITIVE_ENV_HINTS = (
    "PASSWORD",
    "SECRET",
    "TOKEN",
    "API_KEY",
    "ACCESS_KEY",
    "PRIVATE_KEY",
    "CHROMEDRIVER_PATH",
)


def _current_sensitive_values() -> list[str]:
    values: list[str] = []
    for key, value in os.environ.items():
        if not value:
            continue
        upper_key = str(key).upper()
        if any(hint in upper_key for hint in SENSITIVE_ENV_HINTS):
            values.append(str(value))
    return sorted(set(values), key=len, reverse=True)


def redact_sensitive_text(value) -> str:
    text = "" if value is None else str(value)

    for secret in _current_sensitive_values():
        if secret:
            text = text.replace(secret, "[REDACTED]")

    text = re.sub(
        r'(?i)\b(password|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)\b\s*[:=]\s*([^\s,;]+)',
        lambda m: f"{m.group(1)}=[REDACTED]",
        text,
    )
    return text

# ===================== CHECKBOX ICONS =====================

def make_checkbox_images(size: int = 18) -> tuple[tk.PhotoImage, tk.PhotoImage]:
    """Generate (unchecked, checked) checkbox icons using PhotoImage pixels."""
    w = h = size

    def new_img() -> tk.PhotoImage:
        img = tk.PhotoImage(width=w, height=h)
        img.put("#ffffff", to=(0, 0, w, h))
        return img

    unchecked = new_img()
    checked = new_img()

    border = "#000000"
    for x in range(1, w - 1):
        unchecked.put(border, (x, 1))
        unchecked.put(border, (x, h - 2))
        checked.put(border, (x, 1))
        checked.put(border, (x, h - 2))
    for y in range(1, h - 1):
        unchecked.put(border, (1, y))
        unchecked.put(border, (w - 2, y))
        checked.put(border, (1, y))
        checked.put(border, (w - 2, y))

    light = "#f0f0f0"
    dark = "#b0b0b0"
    for x in range(2, w - 2):
        unchecked.put(light, (x, 2))
        checked.put(light, (x, 2))
        unchecked.put(dark, (x, h - 3))
        checked.put(dark, (x, h - 3))
    for y in range(2, h - 2):
        unchecked.put(light, (2, y))
        checked.put(light, (2, y))
        unchecked.put(dark, (w - 3, y))
        checked.put(dark, (w - 3, y))

    mark = "#1a7f37"
    pts = [(5, 10), (6, 11), (7, 12), (8, 11), (9, 10), (10, 9), (11, 8), (12, 7)]
    for (x, y) in pts:
        for dx, dy in [(0, 0), (1, 0), (0, 1)]:
            xx, yy = x + dx, y + dy
            if 0 <= xx < w and 0 <= yy < h:
                checked.put(mark, (xx, yy))

    return unchecked, checked


# ===================== DATA MODEL =====================

@dataclass
class FeatureRecord:
    feature_number: str

    frequency: str
    last_update_str: str
    notes: str
    bugs: str
    clickup_task_id: str
    clickup_status: str

    last_update_dt: Optional[datetime]
    next_due_dt: Optional[datetime]

    last_duration_sec: Optional[float]        # persisted on success only
    last_status_saved: str                    # persisted on every run ("success"/"failure"/"timeout"/"skipped"/"")
    timeout_sec: Optional[int] = None           # optional per-feature timeout (seconds)
    status: Optional[str] = None

    @property
    def last_update_display(self) -> str:
        return self.last_update_dt.strftime("%d.%m.%Y") if self.last_update_dt else ""

    @property
    def next_due_display(self) -> str:
        return self.next_due_dt.strftime("%d.%m.%Y") if self.next_due_dt else ""

    @property
    def last_duration_hms(self) -> str:
        if self.last_duration_sec is None:
            return ""
        total = int(round(self.last_duration_sec))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


def parse_last_update(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def compute_next_due(freq: str, last_dt: Optional[datetime]) -> Optional[datetime]:
    if not freq:
        return None
    delta = FREQUENCY_DELTAS.get(freq.lower())
    if delta is None:
        return None
    return datetime.now() if last_dt is None else last_dt + delta

def norm_key(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "_")


def canonical_workflow_status(s: str) -> str:
    """
    Returns canonical display casing for known workflow statuses,
    otherwise returns original (trimmed) status.
    """
    raw = (s or "").strip()
    if not raw:
        return ""
    return _CANON_STATUS.get(norm_key(raw), raw)

def display_task_sync_status(value: str) -> str:
    """
    Convert internal Task Sync state into user-facing GUI text.

    The persisted workflow state remains unchanged.
    """
    status = str(
        value or ""
    ).strip()

    if status.upper() == "STALE":
        return "Needs update"

    return status


def clickup_daily_sync_is_ready(state: dict, today: str) -> bool:
    return (
        isinstance(state, dict)
        and state.get("status") == "SYNCED"
        and state.get("date") == today
    )


def clickup_daily_sync_report_is_current(
    report: dict,
    session_id: str,
    today: str,
) -> bool:
    if not isinstance(report, dict) or report.get("ok") is not True:
        return False

    try:
        session_start = datetime.strptime(
            session_id[:19], "%Y-%m-%d_%H-%M-%S"
        )
        report_start = datetime.fromisoformat(report["started_at"])
        return (
            report_start.tzinfo is None
            and session_start <= report_start
            and report_start.date().isoformat() == today
        )
    except (ValueError, TypeError, KeyError):
        return False

def clickup_reloaded_records_match(gui_records, disk_records) -> bool:
    """Verify that ClickUp-owned data was actually reloaded."""
    if not isinstance(disk_records, list):
        return False

    if len(gui_records) != len(disk_records):
        return False

    disk_by_feature = {}

    for row in disk_records:
        if not isinstance(row, dict):
            return False

        feature = str(row.get("feature_number", "")).strip()
        if not feature or feature in disk_by_feature:
            return False

        disk_by_feature[feature] = row

    seen = set()

    for record in gui_records:
        feature = str(record.feature_number).strip()

        if not feature or feature in seen:
            return False

        seen.add(feature)
        row = disk_by_feature.get(feature)

        if row is None:
            return False

        comparisons = (
            (
                canonical_workflow_status(record.status),
                canonical_workflow_status(
                    str(row.get("status", "") or "")
                ),
            ),
            (
                str(record.frequency or "").strip().lower(),
                str(row.get("frequency", "") or "").strip().lower(),
            ),
            (
                str(record.clickup_status or "").strip(),
                str(row.get("clickup_status", "") or "").strip(),
            ),
            (
                str(record.clickup_task_id or "").strip(),
                str(row.get("clickup_task_id", "") or "").strip(),
            ),
            (
                str(record.notes or "").strip(),
                str(row.get("notes", "") or "").strip(),
            ),
        )

        if any(actual != expected for actual, expected in comparisons):
            return False

    return True


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """
    Atomic write: write to temp file in same directory, then replace.
    Prevents corrupt JSON if the app crashes mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding=encoding)
    os.replace(tmp_path, path)

def _records_backup_path(path: Path, generation: int) -> Path:
    return path.with_name(f"{path.name}.bak.{generation}")


def _records_backup_candidates(path: Path, keep: int) -> list[Path]:
    candidates = [
        _records_backup_path(path, generation)
        for generation in range(1, keep + 1)
    ]

    # Legacy single-backup filename retained for backwards compatibility.
    legacy = path.with_suffix(".json.bak")
    candidates.append(legacy)

    return candidates


def _rotate_record_backups(path: Path, keep: int = 5) -> None:
    """
    Preserve the previous `keep` generations of a file.

    .bak.1 is newest; .bak.<keep> is oldest.
    """
    if keep <= 0 or not path.exists():
        return

    for generation in range(keep, 1, -1):
        src = _records_backup_path(path, generation - 1)
        dst = _records_backup_path(path, generation)

        if src.exists():
            os.replace(src, dst)

    newest = _records_backup_path(path, 1)
    newest_tmp = path.with_name(f"{path.name}.bak.1.tmp")

    shutil.copy2(path, newest_tmp)
    os.replace(newest_tmp, newest)

def _read_json_file(path: Path) -> Optional[dict]:
    try:
        if not path.exists():
            return None
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None

# ===================== GUI =====================
def acquire_lock(lock_path: Path, timeout_sec: float = 0.0, poll_sec: float = 0.1) -> bool:
    """
    Cross-process lock using an exclusive lock file.

    Stale-lock recovery:
    - if the recorded PID no longer exists, remove the stale lock;
    - if the lock cannot be parsed, remove it only after 10 minutes;
    - never remove a lock belonging to a live process.

    timeout_sec=0 means "do not wait" (non-blocking for Tk thread).
    """
    deadline = time.time() + max(0.0, float(timeout_sec))

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"pid={os.getpid()}\n")
                f.write(f"ts={datetime.now().isoformat(timespec='seconds')}\n")
            return True

        except FileExistsError:
            stale = False

            try:
                text = lock_path.read_text(encoding="utf-8", errors="replace")
                match = re.search(r"(?m)^pid=(\d+)\s*$", text)

                if match:
                    lock_pid = int(match.group(1))

                    # A lock owned by a live process must never be removed.
                    if not psutil.pid_exists(lock_pid):
                        stale = True
                else:
                    # Corrupt/legacy lock: only recover after a generous age.
                    age_sec = time.time() - lock_path.stat().st_mtime
                    if age_sec >= 600:
                        stale = True

            except FileNotFoundError:
                # Lock disappeared between O_EXCL failure and inspection.
                continue

            except Exception:
                # If we cannot confidently prove it is stale, leave it alone.
                stale = False

            if stale:
                try:
                    lock_path.unlink()
                    continue
                except FileNotFoundError:
                    continue
                except Exception:
                    pass

            if time.time() >= deadline:
                return False

            time.sleep(poll_sec)

        except Exception:
            return False

def release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass

class FeatureManagerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Feature Manager")
        # Maximized after persisted GUI state is loaded.

        self.img_unchecked, self.img_checked = make_checkbox_images(size=18)

        self.records: List[FeatureRecord] = []
        self.feature_map: Dict[str, FeatureRecord] = {}
        self.selected_features: set[str] = set()
        self.last_run_results: Dict[str, RunResult] = {}

        self.filter_frequency = tk.StringVar(value="All")
        self.filter_status = tk.StringVar(value="All")
        self.filter_clickup_status = tk.StringVar(value="All")
        self.filter_selected = tk.StringVar(value="All")
        self.filter_last_status_saved = tk.StringVar(value="All")
        self.filter_last_status_session = tk.StringVar(value="All")
        self.search_var = tk.StringVar(value="")
        self.notes_search_var = tk.StringVar(value="")
        self.selected_count_var = tk.StringVar(value="Selected: 0")
        # Header-click sorting state
        self.sort_col = "feature"  # default
        self.sort_reverse = False  # False=ascending, True=descending
        self.runtime_settings = worker_runtime_settings(__file__)
        self.script_runs_dir_var = tk.StringVar(value=str(DEFAULT_SCRIPT_RUNS_DIR))
        self.max_workers_var = tk.StringVar(value="4")

        self.pack_finished_once = False
        self.btn_copy_zips = None

        self.cancel_event = threading.Event()
        self.progress_var = tk.StringVar(value="")
        self.btn_cancel = None
        self.btn_hard_cancel = None
        self.current_action = None  # e.g. "Run" when run is active
        self.sync_in_progress = False
        self.require_reload_after_sync = False
        self.sync_modal = None

        self.work_ledger: Optional[WorkLedger] = None
        self.work_ledger_ready = False
        self.work_ledger_error: Optional[str] = None
        self.active_work_intervals: Dict[str, WorkInterval] = {}
        self._work_jobs_pending = 0
        self._close_requested = False
        self.feature_setup_states: Dict[str, str] = {}
        self.sqlite_compare_states: Dict[str, str] = {}
        self.batch_metadata: Dict[str, dict] = {}
        self.clickup_state = ClickUpStateStore(STORAGE.path("clickup_state"))
        self.clickup_sync_state = self.clickup_state.read()
        self.clickup_daily_sync_state_path = STORAGE.path("clickup_daily_state")
        self.clickup_daily_sync_state = (
            _read_json_file(self.clickup_daily_sync_state_path) or {}
        )
        self.clickup_daily_sync_ready = clickup_daily_sync_is_ready(
            self.clickup_daily_sync_state, date.today().isoformat()
        )
        self._clickup_sync_pending_reload = False
        self._clickup_sync_completed_date = None
        self._setup_refresh_after = None

        # Thread-safe UI event queue (worker threads -> Tk main thread)
        self._ui_queue = Queue()
        self._ui_queue_max_per_tick = 200
        self.after(50, self._drain_ui_queue)

        # records.json persistence: deferred save queue + external change detection
        self._records_save_pending = False
        self._records_save_retry_scheduled = False
        self._records_save_backoff_ms = 300
        self._records_saving_now = False

        self._records_mtime_loaded = 0.0
        self.records_version_loaded = 0
        self.records_version_current = 0

        # Periodic check for external edits (Sync/manual edits)
        self.after(2000, self._check_records_external_change)


        self.btn_run = None
        self.btn_rerun_failed = None
        self.btn_pack = None
        self.btn_batch = None
        self.btn_reload = None
        self.btn_select_all = None
        self.btn_select_none = None
        self.btn_invert = None
        self.btn_sync_clickup_tasks = None
        self.btn_validate = None
        self.btn_clean_dirs = None
        self._setup_styles()
        self._build_ui()
        self._load_data()
        self._load_batch_metadata()
        self._refresh_table()
        self._rotate_run_history_if_needed()
        self.after(0, self._refresh_feature_setup_states)

        self._load_gui_state()
        self._restore_canonical_approvals()
        self._refresh_table()
        self._maximize_window(self)
        self._apply_column_visibility()
        self._update_dataops_sync_display()
        self.after(150, self._run_startup_health_check)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(0, self._initialize_work_ledger)

    # ---------- styles ----------

    def _setup_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("Treeview", rowheight=28, borderwidth=1, relief="solid")
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#cfe8ff")], foreground=[("selected", "black")])

    # ---------- file reading ----------

    # ---------- data load/save ----------

    def _load_data(self):

        self.records.clear()
        self.feature_map.clear()

        items = []
        if RECORDS_JSON.exists():
            try:
                items = json.loads(RECORDS_JSON.read_text(encoding="utf-8"))
                if not isinstance(items, list):
                    try:
                        self._log("Warning: records.json root is not a list; ignoring file contents.")
                    except Exception:
                        pass
                    items = []
                    try:
                        self._records_mtime_loaded = RECORDS_JSON.stat().st_mtime
                    except Exception:
                        self._records_mtime_loaded = 0.0
                    self._load_records_meta()


            except Exception as e:

                # Attempt recovery from newest valid backup generation.

                recovered = False

                recovery_source = None

                items = []

                for bak in _records_backup_candidates(

                        RECORDS_JSON,

                        RECORDS_BACKUP_KEEP,

                ):

                    try:

                        if not bak.exists():
                            continue

                        backup_text = bak.read_text(encoding="utf-8")

                        candidate = json.loads(backup_text)

                        if not isinstance(candidate, list):
                            continue

                        # Restore without consuming/deleting the backup itself.

                        atomic_write_text(

                            RECORDS_JSON,

                            backup_text,

                            encoding="utf-8",

                        )

                        items = candidate

                        recovered = True

                        recovery_source = bak

                        break


                    except Exception:

                        continue

                if recovered:

                    try:

                        self._log(

                            f"Recovered records.json from "

                            f"{recovery_source.name} after a read error."

                        )

                    except Exception:

                        pass

                else:

                    messagebox.showerror(

                        "Error",

                        f"Failed to read records.json and no valid backup "

                        f"could be recovered:\n{e}",

                    )

                    items = []

        if RECORDS_JSON.exists():
            try:
                self._records_mtime_loaded = RECORDS_JSON.stat().st_mtime
            except Exception:
                self._records_mtime_loaded = 0.0
            self._load_records_meta()

        if not RECORDS_JSON.exists():
            self._records_mtime_loaded = 0.0
            self._load_records_meta()

        for item in items:
            if not isinstance(item, dict):
                try:
                    self._log("Warning: records.json contains a non-object item; skipping.")
                except Exception:
                    pass
                continue

            feature = str(item.get("feature_number", "")).strip()
            if not feature:
                continue

            freq_raw = str(item.get("frequency", "")).strip()
            freq = freq_raw.lower()
            if freq and freq not in ALLOWED_FREQUENCIES:
                try:
                    self._log(f"Warning: feature_{feature} has unknown frequency '{freq_raw}'.")
                except Exception:
                    pass

            status_raw = str(item.get("status", "")).strip()
            status = canonical_workflow_status(status_raw)
            if status_raw and status == status_raw and norm_key(status_raw) not in _CANON_STATUS:
                try:
                    self._log(f"Warning: feature_{feature} has unknown workflow status '{status_raw}'.")
                except Exception:
                    pass

            last_update_str = str(item.get("last_update", "")).strip()
            last_dt = parse_last_update(last_update_str)
            if last_update_str and last_dt is None:
                try:
                    self._log(f"Warning: feature_{feature} has invalid last_update '{last_update_str}'.")
                except Exception:
                    pass

            next_due = compute_next_due(freq, last_dt)

            dur = item.get("last_duration_sec", None)
            dur_val: Optional[float]
            if isinstance(dur, (int, float)):
                dur_val = float(dur)
            else:
                dur_val = None

            last_status_saved = str(item.get("last_status", "")).strip().lower()
            if last_status_saved not in ALLOWED_LAST_RUN_STATUSES:
                try:
                    self._log(f"Warning: feature_{feature} has unknown last_status '{item.get('last_status')}'.")
                except Exception:
                    pass
            # Optional per-feature timeout policy (seconds)
            clickup_task_id = str(
                item.get("clickup_task_id", "") or ""
            ).strip()
            clickup_status = str(
                item.get("clickup_status", "") or ""
            ).strip()
            notes = str(item.get("notes", "") or "").strip()

            bugs = str(item.get("bugs", "none") or "none").strip().lower()
            if bugs not in {"open", "fixed", "none"}:
                try:
                    self._log(
                        f"Warning: feature_{feature} has unknown bugs value "
                        f"'{item.get('bugs')}'. Defaulting to 'none'."
                    )
                except Exception:
                    pass
                bugs = "none"
            timeout_val = None
            raw_timeout = item.get("timeout_sec", None)
            if isinstance(raw_timeout, (int, float)):
                try:
                    t = int(raw_timeout)
                    timeout_val = t if t > 0 else None
                except Exception:
                    timeout_val = None
            rec = FeatureRecord(
                feature_number=feature,
                status=status,
                frequency=freq,
                last_update_str=last_update_str,
                notes=notes,
                bugs=bugs,
                clickup_task_id=clickup_task_id,
                clickup_status=clickup_status,
                last_update_dt=last_dt,
                next_due_dt=next_due,
                last_duration_sec=dur_val,
                last_status_saved=last_status_saved,
                timeout_sec=timeout_val,
            )
            self.records.append(rec)
            self.feature_map[feature] = rec

    def _load_batch_metadata(self):
        try:
            data = json.loads(BATCH_METADATA_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.batch_metadata = {
                    str(feature): item for feature, item in data.items() if isinstance(item, dict)
                }
        except (OSError, ValueError, TypeError):
            self.batch_metadata = {}

    def _load_gui_state(self):
        if not GUI_STATE_JSON.exists():
            return
        try:
            data = json.loads(GUI_STATE_JSON.read_text(encoding="utf-8"))
        except Exception:
            return

        # Restore common fields if present
        self.script_runs_dir_var.set(data.get("script_runs_dir", self.script_runs_dir_var.get()))
        self.max_workers_var.set(str(data.get("max_workers", self.max_workers_var.get())))

        self.filter_frequency.set(data.get("filter_frequency", self.filter_frequency.get()))
        self.filter_status.set(data.get("filter_status", self.filter_status.get()))
        self.filter_clickup_status.set(data.get("filter_clickup_status", self.filter_clickup_status.get(),))

        self.filter_last_status_saved.set(data.get("filter_last_status_saved", self.filter_last_status_saved.get()))
        self.filter_last_status_session.set(data.get("filter_last_status_session", self.filter_last_status_session.get()))

        self.search_var.set(data.get("search_text", self.search_var.get()))
        self.notes_search_var.set(
            data.get("notes_search_text", self.notes_search_var.get())
        )
        stored_columns = data.get("column_visibility")
        if isinstance(stored_columns, dict):
            self.column_visibility = {
                str(name): bool(visible) for name, visible in stored_columns.items()
            }
        geom = data.get("geometry")
        if isinstance(geom, str) and geom:
            try:
                self.geometry(geom)
            except Exception:
                pass

    def _save_gui_state(self):
        data = {
            "script_runs_dir": self.script_runs_dir_var.get(),
            "max_workers": self.max_workers_var.get(),
            "filter_frequency": self.filter_frequency.get(),
            "filter_status": self.filter_status.get(),
            "filter_clickup_status": self.filter_clickup_status.get(),
            "filter_last_status_saved": self.filter_last_status_saved.get(),
            "filter_last_status_session": self.filter_last_status_session.get(),
            "search_text": self.search_var.get(),
            "notes_search_text": self.notes_search_var.get(),
            "geometry": self.geometry(),
            "column_visibility": {
                name: bool(variable.get()) for name, variable in getattr(self, "column_visibility_vars", {}).items()
            },
        }

        try:
            atomic_write_text(GUI_STATE_JSON, json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _apply_column_visibility(self):
        if not hasattr(self, "tree"):
            return
        columns = tuple(self.tree["columns"])
        self.column_visibility = normalize_column_visibility(columns, self.column_visibility)
        for name, variable in self.column_visibility_vars.items():
            variable.set(self.column_visibility[name])
        self.tree.configure(displaycolumns=visible_columns(columns, self.column_visibility))
        self.after_idle(self._position_table_filter_entries)

    def _set_column_visibility(self, name):
        self.column_visibility[name] = bool(self.column_visibility_vars[name].get())
        self._apply_column_visibility()
        self._save_gui_state()

    def _show_all_columns(self):
        for variable in self.column_visibility_vars.values():
            variable.set(True)
        self.column_visibility = {name: True for name in self.column_visibility_vars}
        self._apply_column_visibility()
        self._save_gui_state()

    def _reset_columns_default(self):
        defaults = default_column_visibility(self.column_visibility_vars)
        for name, variable in self.column_visibility_vars.items():
            variable.set(defaults[name])
        self.column_visibility = defaults
        self._apply_column_visibility()
        self._save_gui_state()

    def _close_columns_popup(self):
        popup = getattr(self, "columns_popup", None)
        self.columns_popup = None

        if popup is None:
            return

        try:
            if popup.winfo_exists():
                popup.destroy()
        except Exception:
            pass

    def _toggle_columns_popup(self):
        popup = getattr(self, "columns_popup", None)

        try:
            if popup is not None and popup.winfo_exists():
                self._close_columns_popup()
                return
        except Exception:
            self.columns_popup = None

        self._open_columns_popup()

    def _open_columns_popup(self):
        self._close_columns_popup()

        popup = tk.Toplevel(self)
        self.columns_popup = popup

        # Keep this looking and behaving like a dropdown panel rather than
        # a separate application window.
        popup.withdraw()
        popup.overrideredirect(True)
        popup.transient(self)

        panel = ttk.Frame(
            popup,
            padding=6,
            relief="solid",
            borderwidth=1,
        )
        panel.pack(fill=tk.BOTH, expand=True)

        for column in self.tree["columns"]:
            variable = self.column_visibility_vars[column]
            ttk.Checkbutton(
                panel,
                text=self._base_headings.get(column, column),
                variable=variable,
                command=lambda name=column: self._set_column_visibility(name),
            ).pack(
                anchor=tk.W,
                fill=tk.X,
                padx=2,
                pady=1,
            )

        ttk.Separator(panel, orient="horizontal").pack(
            fill=tk.X,
            pady=(5, 4),
        )

        actions = ttk.Frame(panel)
        actions.pack(fill=tk.X)

        ttk.Button(
            actions,
            text="Show all",
            command=self._show_all_columns,
        ).pack(
            side=tk.LEFT,
            padx=(0, 4),
        )

        ttk.Button(
            actions,
            text="Reset to default view",
            command=self._reset_columns_default,
        ).pack(
            side=tk.LEFT,
            padx=4,
        )

        ttk.Button(
            actions,
            text="Close",
            command=self._close_columns_popup,
        ).pack(
            side=tk.RIGHT,
            padx=(4, 0),
        )

        popup.bind(
            "<Escape>",
            lambda _event: self._close_columns_popup(),
        )

        # Position directly beneath the Columns button.
        popup.update_idletasks()

        try:
            x = self.columns_menu_button.winfo_rootx()
            y = (
                self.columns_menu_button.winfo_rooty()
                + self.columns_menu_button.winfo_height()
            )
            popup.geometry(f"+{x}+{y}")
        except Exception:
            pass

        popup.deiconify()
        popup.lift()

    def _rotate_run_history_if_needed(self) -> None:
        """
        Prevent run_history.jsonl from growing forever:
        - If file exceeds RUN_HISTORY_MAX_MB, archive it and keep last RUN_HISTORY_KEEP_LINES lines.
        Runs on Tk thread but does small/fast checks; heavy I/O is guarded and can fail safely.
        """
        path = RUN_HISTORY_JSONL
        try:
            if not path.exists():
                return

            size_bytes = path.stat().st_size
            if size_bytes <= int(RUN_HISTORY_MAX_MB * 1024 * 1024):
                return

            # Read last N lines without loading full file (best-effort).
            keep = []
            try:
                # Simple tail implementation: read chunks from end.
                with path.open("rb") as f:
                    f.seek(0, os.SEEK_END)
                    end = f.tell()
                    chunk_size = 64 * 1024
                    data = b""
                    pos = end
                    while pos > 0 and data.count(b"\n") <= RUN_HISTORY_KEEP_LINES:
                        read_size = chunk_size if pos >= chunk_size else pos
                        pos -= read_size
                        f.seek(pos, os.SEEK_SET)
                        data = f.read(read_size) + data
                    lines = data.splitlines()[-RUN_HISTORY_KEEP_LINES:]
                    keep = [ln.decode("utf-8", errors="replace") for ln in lines]
            except Exception:
                keep = []

            # Archive full file first (best-effort).
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            archive_path = STORAGE.run_history_archive_dir / f"run_history_{stamp}.jsonl"
            try:
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                if archive_path.exists():
                    raise FileExistsError(f"Run-history archive already exists: {archive_path}")
                os.replace(path, archive_path)
            except Exception:
                # If replace fails, don't risk data loss.
                self._log("run_history.jsonl rotation skipped (unable to archive).")
                return

            # Write trimmed active file (can be empty if tail failed).
            try:
                text = "\n".join(keep).rstrip("\n")
                if text:
                    text += "\n"
                atomic_write_text(path, text, encoding="utf-8")
                self._log(f"Rotated run_history.jsonl -> {archive_path.name} (kept last {len(keep)} lines).")
                self._rotate_sync_transcripts_if_needed()
                self._prune_gui_actions_if_needed()
            except Exception as e:
                self._log(f"run_history.jsonl rotation: failed to write new file: {e}")

        except Exception:
            # Never crash GUI for history maintenance.
            return

    def _request_save_records(self) -> None:
        """
        Coalesce record saves. If records.json is locked, keep retrying in the background
        (Tk thread via after) until a save succeeds.
        """
        self._records_save_pending = True
        if not self._records_save_retry_scheduled:
            self._records_save_retry_scheduled = True
            self.after(50, self._save_records)

    def _save_records(self):
        # Tk thread only (scheduled via _request_save_records / after)
        if not getattr(self, "_records_save_pending", False):
            self._records_save_retry_scheduled = False
            return

        if getattr(self, "_records_saving_now", False):
            return

        payload = []
        for rec in self.records:
            last_update = rec.last_update_dt.isoformat(timespec="seconds") if rec.last_update_dt else (
                        rec.last_update_str or "")
            obj = {
                "feature_number": rec.feature_number,
                "status": rec.status,
                "frequency": rec.frequency,
                "last_update": last_update,
                "clickup_task_id": rec.clickup_task_id,
                "clickup_status": rec.clickup_status,
            }
            if rec.last_duration_sec is not None:
                obj["last_duration_sec"] = rec.last_duration_sec

            if rec.last_status_saved:
                obj["last_status"] = rec.last_status_saved

            obj["notes"] = rec.notes
            obj["bugs"] = rec.bugs

            payload.append(obj)

        # Non-blocking lock attempt; if locked, retry until success.
        if not acquire_lock(RECORDS_LOCK, timeout_sec=0.0):
            self._records_save_backoff_ms = min(int(self._records_save_backoff_ms * 1.5), 2000)
            self.after(self._records_save_backoff_ms, self._save_records)
            return

        self._records_saving_now = True
        try:
            # Refuse to overwrite records changed outside this GUI.
            disk_mtime = (
                RECORDS_JSON.stat().st_mtime
                if RECORDS_JSON.exists() else 0.0
            )
            disk_meta = _read_json_file(RECORDS_META_JSON) or {}

            try:
                disk_version = int(
                    disk_meta.get("records_version", 0)
                )
            except (TypeError, ValueError):
                disk_version = 0

            if (
                disk_mtime != self._records_mtime_loaded
                or disk_version != self.records_version_loaded
            ):
                self._records_save_pending = False
                self.require_reload_after_sync = True
                self._log(
                    "BLOCKED stale GUI save: records.json changed "
                    "externally. Click Reload before continuing."
                )
                self._set_main_buttons_state(
                    enabled=False, only_reload=True
                )
                return
            # Best-effort five-generation backup rotation.
            try:
                _rotate_record_backups(RECORDS_JSON, RECORDS_BACKUP_KEEP)
            except Exception:
                pass

            atomic_write_text(RECORDS_JSON, json.dumps(payload, indent=4), encoding="utf-8")

            # Success: clear pending and reset backoff
            self._records_save_pending = False
            self._records_save_backoff_ms = 300

            # Track mtime/version (for external change detection)
            try:
                self._records_mtime_loaded = RECORDS_JSON.stat().st_mtime
            except Exception:
                pass

            try:
                self._bump_records_version()
            except Exception:
                pass

        except Exception as e:
            messagebox.showerror("Error", f"Failed to write records.json: {e}")
        finally:
            self._records_saving_now = False
            release_lock(RECORDS_LOCK)

            if self._records_save_pending:
                self.after(300, self._save_records)
            else:
                self._records_save_retry_scheduled = False

    # ---------- UI build ----------

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=6)

        ttk.Label(top, text="Script Runs dir:").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.script_runs_dir_var, width=70).pack(side=tk.LEFT, padx=5)
        self.script_runs_dir_var.trace_add("write", self._schedule_feature_setup_refresh)

        def browse_dir():
            initial = self.script_runs_dir_var.get()
            chosen = filedialog.askdirectory(initialdir=initial or str(APP_ROOT))
            if chosen:
                self.script_runs_dir_var.set(chosen)

        ttk.Button(top, text="Browse...", command=browse_dir).pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(top, text="Max workers:").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.max_workers_var, width=5).pack(side=tk.LEFT, padx=(5, 15))

        def _on_search_change(*_):
            self._refresh_table()

        self.search_var.trace_add("write", _on_search_change)
        self.notes_search_var.trace_add("write", _on_search_change)

        # Workflow menus and status controls.
        new_controls = ttk.Frame(self)
        new_controls.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 6))

        btns = ttk.Frame(self)
        btns.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 6))

        self.btn_run = ttk.Button(btns, text="Run selected", command=self._start_run_selected)
        self.btn_run.pack(side=tk.LEFT, padx=5)
        self.btn_rerun_failed = ttk.Button(btns, text="Rerun failed", command=self._rerun_failed,)
        self.btn_rerun_failed.pack(side=tk.LEFT, padx=5)

        self.btn_pack = ttk.Button(btns, text="Pack selected", command=self._start_pack_selected)
        self.btn_pack.pack(side=tk.LEFT, padx=5)

        self.btn_copy_zips = ttk.Button(
            btns,
            text="Copy packed zips",
            command=self._start_copy_packed_zips,
            state="disabled",
        )
        self.btn_copy_zips.pack(side=tk.LEFT, padx=5)

        self.btn_batch = ttk.Button(btns, text="Batch outputs for selected", command=self._start_batch_selected)
        self.btn_batch.pack(side=tk.LEFT, padx=5)

        self.work_menu = tk.Menu(self, tearoff=False)
        self.work_menu.add_command(label="Start Work", command=self._start_work_selected, state="disabled")
        self.work_menu.add_command(label="Stop Work", command=self._stop_work_selected, state="disabled")
        self.work_menu.add_separator()
        self.work_menu.add_command(label="Initialize selected", command=self._initialize_selected_features)
        self.work_menu.add_command(label="Start Development", command=self._start_development_selected, state="disabled")
        self.work_menu.add_separator()
        self.work_menu.add_command(label="Review Work Ledger", command=self._open_work_ledger, state="disabled")
        self.work_menu.add_command(label="Manage Work Categories", command=self._manage_work_categories, state="disabled")
        self.work_menu_button = ttk.Menubutton(new_controls, text="Work", menu=self.work_menu)
        self.work_menu_button.pack(side=tk.LEFT, padx=5)
        self.sqlite_menu = tk.Menu(self, tearoff=False)
        self.sqlite_menu.add_command(label="Compare selected", command=self._compare_selected_features)
        self.sqlite_menu.add_command(
            label='Run SQLite Comparison Demo',
            command=self._run_sqlite_comparison_demo,
        )
        self.sqlite_menu_button = ttk.Menubutton(new_controls, text="SQLite", menu=self.sqlite_menu)
        self.sqlite_menu_button.pack(side=tk.LEFT, padx=5)
        self.column_visibility_vars = {}
        self.column_visibility = {}
        self.columns_popup = None
        self.columns_menu_button = ttk.Button(
            new_controls,
            text="Columns ▼",
            command=self._toggle_columns_popup,
        )
        self.columns_menu_button.pack(side=tk.LEFT, padx=5)
        self.clickup_menu = tk.Menu(self, tearoff=False)
        self.clickup_menu.add_command(
            label="Update tasks in clickup",
            command=self._attach_selected_outputs,
        )
        self.clickup_menu.add_separator()
        self.clickup_menu.add_command(label="End Workday / Sync ClickUp", command=self._start_end_workday_review, state="disabled")
        self.clickup_menu.add_command(label="Weekly reconciliation", command=self._show_weekly_reconciliation, state="disabled")
        self.clickup_menu_button = ttk.Menubutton(new_controls, text="ClickUp", menu=self.clickup_menu)
        self.clickup_menu_button.pack(side=tk.LEFT, padx=5)
        self.python_settings_button = ttk.Button(
            new_controls,
            text="Python Settings",
            command=self._open_python_settings,
        )
        self.python_settings_button.pack(
            side=tk.LEFT,
            padx=5,
        )

        self.demo_button = ttk.Button(
            new_controls,
            text="Run Demo",
            command=self._run_bundled_demo,
        )
        self.demo_button.pack(
            side=tk.LEFT,
            padx=5,
        )


        self.btn_sync_clickup_tasks = ttk.Button(
            new_controls,
            text="Sync data from clickup",
            command=self._start_sync_clickup_task_ids,
        )
        self.btn_sync_clickup_tasks.pack(
            side=tk.LEFT,
            padx=5,
        )
        self.work_status_var = tk.StringVar(value="Work tracking: starting")
        ttk.Label(new_controls, textvariable=self.work_status_var).pack(side=tk.LEFT, padx=8)
        self.dataops_sync_status_var = tk.StringVar(value="ClickUp data sync: checking")
        ttk.Label(new_controls, textvariable=self.dataops_sync_status_var).pack(side=tk.LEFT, padx=8)

        self.btn_validate = ttk.Button(btns, text="Validate selected", command=self._start_validate_selected)
        self.btn_validate.pack(side=tk.LEFT, padx=5)

        self.btn_reload = ttk.Button(btns, text="Reload", command=self._reload)
        self.btn_reload.pack(side=tk.LEFT, padx=5)

        self.btn_clean_dirs = ttk.Button(btns, text="Clean directories", command=self._start_clean_directories_selected)
        self.btn_clean_dirs.pack(side=tk.LEFT, padx=5)

        self.btn_select_all = ttk.Button(btns, text="Select all", command=self._select_all_filtered)
        self.btn_select_all.pack(side=tk.LEFT, padx=5)

        self.btn_select_none = ttk.Button(btns, text="Select none", command=self._select_none_filtered)
        self.btn_select_none.pack(side=tk.LEFT, padx=5)

        self.btn_invert = ttk.Button(btns, text="Invert", command=self._invert_selection_filtered)
        self.btn_invert.pack(side=tk.LEFT, padx=5)

        ttk.Label(btns, textvariable=self.selected_count_var).pack(side=tk.RIGHT, padx=8)
        ttk.Button(btns, text="Copy diagnostics", command=self._copy_diagnostics_to_clipboard).pack(side=tk.RIGHT, padx=5)
        table_frame = ttk.Frame(self)
        table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=6)

        cols = (
            "feature",
            "status",
            "clickup_status",
            "frequency",
            "last_update",
            "next_due",
            "notes",
            "bugs",
            "last_duration",
            "last_status_saved",
            "last_status_session",
            "run_duration_session",
            "sqlite_copied_session",
            "timeout_sec",
            "work_timer",
            "setup_state",
            "sqlite_compare",
            "person_count",
            "output_class",
            "clickup_task",
            "attachment_status",
            "task_sync",
            "time_sync",
        )

        self.tree = ttk.Treeview(table_frame, columns=cols, show="tree headings", selectmode="browse")

        self.tree.heading("#0", text="Selected", command=lambda: self._on_heading_click("selected"))
        self.tree.column("#0", width=85, anchor=tk.CENTER)
        self.tree.bind("<Button-3>", self._on_tree_heading_right_click)
        self.tree.bind("<ButtonRelease-3>", self._on_tree_heading_right_click)
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        widths = {
            "feature": 95,
            "status": 115,
            "clickup_status": 125,
            "frequency": 105,
            "last_update": 115,
            "next_due": 115,
            "notes": 320,
            "last_duration": 115,
            "bugs": 80,
            "last_status_saved": 150,
            "last_status_session": 160,
            "run_duration_session": 160,
            "sqlite_copied_session": 130,
            "timeout_sec": 110,
            "work_timer": 135,
            "setup_state": 145,
            "sqlite_compare": 145,
            "person_count": 110,
            "output_class": 120,
            "clickup_task": 120,
            "attachment_status": 180,
            "task_sync": 110,
            "time_sync": 110,
        }

        headings = {
            "feature": "Feature",
            "status": "Status",
            "clickup_status": "ClickUp Status",
            "frequency": "Frequency",
            "last_update": "Last Updated",
            "next_due": "Next Due",
            "notes": "Notes",
            "bugs": "Bug",
            "last_duration": "Last Duration",
            "last_status_saved": "Last Run Result",
            "last_status_session": "Current Run Result",
            "run_duration_session": "Duration (this session)",
            "sqlite_copied_session": "SQLite copied?",
            "timeout_sec": "Timeout (sec)",
            "work_timer": "Work Timer",
            "setup_state": "Setup State",
            "sqlite_compare": "SQLite Compare",
            "person_count": "Person Count",
            "output_class": "Output Class",
            "clickup_task": "ClickUp Task",
            "attachment_status": "Attachment Status",
            "task_sync": "Task Sync",
            "time_sync": "Time Sync",
        }

        # Store base heading titles (without sort arrows)
        self._base_headings = headings.copy()

        for c in cols:
            self.tree.heading(
                c,
                text=headings.get(c, c),
                command=lambda col=c: self._on_heading_click(col),
            )
            self.tree.column(c, width=widths.get(c, 120), anchor=tk.CENTER)
        default_columns = default_column_visibility(cols)
        for column in cols:
            default_visible = default_columns[column]
            self.column_visibility_vars[column] = tk.BooleanVar(
                value=default_visible
            )
        # Notes starts wider than the other columns and remains
        # manually resizable through the normal Treeview divider.
        self.tree.column(
            "notes",
            width=320,
            minwidth=120,
            anchor=tk.W,
            stretch=True,
        )
        self.feature_filter_entry = ttk.Entry(
            self.tree,
            textvariable=self.search_var,
        )

        self.notes_filter_entry = ttk.Entry(
            self.tree,
            textvariable=self.notes_search_var,
        )
        self.tree.tag_configure("even", background="#ffffff")
        self.tree.tag_configure("odd", background="#f5f5f5")
        self.tree.tag_configure("failure_even", background="#ffd9d9")
        self.tree.tag_configure("failure_odd", background="#f7c7c7")
        self.tree.tag_configure("filter_row", background="#eeeeee")

        def _tree_yview(*args):
            self.tree.yview(*args)
            self.after_idle(self._position_table_filter_entries)

        def _tree_xview(*args):
            self.tree.xview(*args)
            self.after_idle(self._position_table_filter_entries)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=_tree_yview)
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=_tree_xview)

        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind(
            "<ButtonRelease-1>",
            lambda _event: self.after_idle(self._position_table_filter_entries),
            add="+",
        )
        self.tree.bind(
            "<Configure>",
            lambda _event: self.after_idle(self._position_table_filter_entries),
            add="+",
        )
        self.tree.bind(
            "<MouseWheel>",
            lambda _event: self.after_idle(self._position_table_filter_entries),
            add="+",
        )
        self.tree.bind(
            "<Button-4>",
            lambda _event: self.after_idle(self._position_table_filter_entries),
            add="+",
        )
        self.tree.bind(
            "<Button-5>",
            lambda _event: self.after_idle(self._position_table_filter_entries),
            add="+",
        )

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=10, pady=(0, 6))

        prog = ttk.Frame(self)
        prog.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 6))

        ttk.Label(prog, textvariable=self.progress_var).pack(side=tk.LEFT)

        self.btn_cancel = ttk.Button(prog, text="Cancel", state="disabled", command=self._cancel_current_action)
        self.btn_cancel.pack(side=tk.RIGHT, padx=(6, 0))

        self.btn_hard_cancel = ttk.Button(prog, text="Hard Cancel Now", state="disabled", command=self._hard_cancel_now)
        self.btn_hard_cancel.pack(side=tk.RIGHT)

        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(side=tk.BOTTOM, fill=tk.BOTH, padx=10, pady=6)

        self.log_text = tk.Text(log_frame, height=8, wrap="word")
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._update_heading_labels()

    # ---------- helpers ----------

    def _maximize_window(self, window) -> None:
        """
        Open a large application window inside the usable monitor work area.

        On Windows, this deliberately does not use state("zoomed"). Instead,
        it sizes the window to the monitor work area while leaving a small
        visible margin and accounting for the title bar/window frame.
        """

        def apply_work_area():
            try:
                if not window.winfo_exists():
                    return
            except Exception:
                return

            try:
                window.update_idletasks()
            except Exception:
                pass

            if os.name == "nt":
                try:
                    import ctypes

                    class RECT(ctypes.Structure):
                        _fields_ = [
                            ("left", ctypes.c_long),
                            ("top", ctypes.c_long),
                            ("right", ctypes.c_long),
                            ("bottom", ctypes.c_long),
                        ]

                    class MONITORINFO(ctypes.Structure):
                        _fields_ = [
                            ("cbSize", ctypes.c_ulong),
                            ("rcMonitor", RECT),
                            ("rcWork", RECT),
                            ("dwFlags", ctypes.c_ulong),
                        ]

                    user32 = ctypes.windll.user32

                    user32.GetParent.argtypes = [
                        ctypes.c_void_p,
                    ]
                    user32.GetParent.restype = (
                        ctypes.c_void_p
                    )

                    user32.MonitorFromWindow.argtypes = [
                        ctypes.c_void_p,
                        ctypes.c_uint,
                    ]
                    user32.MonitorFromWindow.restype = (
                        ctypes.c_void_p
                    )

                    user32.GetMonitorInfoW.argtypes = [
                        ctypes.c_void_p,
                        ctypes.POINTER(
                            MONITORINFO
                        ),
                    ]
                    user32.GetMonitorInfoW.restype = (
                        ctypes.c_int
                    )

                    user32.GetWindowRect.argtypes = [
                        ctypes.c_void_p,
                        ctypes.POINTER(
                            RECT
                        ),
                    ]
                    user32.GetWindowRect.restype = (
                        ctypes.c_int
                    )

                    user32.GetClientRect.argtypes = [
                        ctypes.c_void_p,
                        ctypes.POINTER(
                            RECT
                        ),
                    ]
                    user32.GetClientRect.restype = (
                        ctypes.c_int
                    )

                    window.state(
                        "normal"
                    )

                    window.update_idletasks()

                    tk_hwnd = ctypes.c_void_p(
                        window.winfo_id()
                    )

                    wrapper_hwnd = (
                            user32.GetParent(
                                tk_hwnd
                            )
                            or tk_hwnd
                    )

                    MONITOR_DEFAULTTONEAREST = 2

                    monitor = (
                        user32.MonitorFromWindow(
                            wrapper_hwnd,
                            MONITOR_DEFAULTTONEAREST,
                        )
                    )

                    monitor_info = MONITORINFO()
                    monitor_info.cbSize = (
                        ctypes.sizeof(
                            MONITORINFO
                        )
                    )

                    if not user32.GetMonitorInfoW(
                            monitor,
                            ctypes.byref(
                                monitor_info
                            ),
                    ):
                        raise ctypes.WinError()

                    outer_rect = RECT()
                    client_rect = RECT()

                    if not user32.GetWindowRect(
                            wrapper_hwnd,
                            ctypes.byref(
                                outer_rect
                            ),
                    ):
                        raise ctypes.WinError()

                    if not user32.GetClientRect(
                            wrapper_hwnd,
                            ctypes.byref(
                                client_rect
                            ),
                    ):
                        raise ctypes.WinError()

                    outer_width = (
                            outer_rect.right
                            - outer_rect.left
                    )

                    outer_height = (
                            outer_rect.bottom
                            - outer_rect.top
                    )

                    client_width = (
                            client_rect.right
                            - client_rect.left
                    )

                    client_height = (
                            client_rect.bottom
                            - client_rect.top
                    )

                    nonclient_width = max(
                        0,
                        outer_width
                        - client_width,
                    )

                    nonclient_height = max(
                        0,
                        outer_height
                        - client_height,
                    )

                    work = monitor_info.rcWork

                    work_width = (
                            work.right
                            - work.left
                    )

                    work_height = (
                            work.bottom
                            - work.top
                    )

                    inset = 8

                    target_outer_width = max(
                        640,
                        work_width
                        - (inset * 2),
                    )

                    target_outer_height = max(
                        480,
                        work_height
                        - (inset * 2),
                    )

                    target_client_width = max(
                        600,
                        target_outer_width
                        - nonclient_width,
                    )

                    target_client_height = max(
                        400,
                        target_outer_height
                        - nonclient_height,
                    )

                    x = (
                            work.left
                            + inset
                    )

                    y = (
                            work.top
                            + inset
                    )

                    window.geometry(
                        (
                            f"{target_client_width}"
                            f"x{target_client_height}"
                            f"+{x}+{y}"
                        )
                    )

                    window.update_idletasks()

                    return

                except Exception:
                    pass

            # Portable fallback when the Windows work-area API is unavailable.
            try:
                width = max(
                    640,
                    window.winfo_screenwidth()
                    - 32,
                )

                height = max(
                    480,
                    window.winfo_screenheight()
                    - 96,
                )

                window.geometry(
                    f"{width}x{height}+8+8"
                )

            except Exception:
                try:
                    window.state(
                        "zoomed"
                    )
                except Exception:
                    pass

        try:
            window.after_idle(
                apply_work_area
            )

        except Exception:
            apply_work_area()

    def _try_begin_busy(self, action: str) -> bool:
        """
        UI-thread only. Prevent overlapping actions (Run/Pack/Batch/Copy/Sync).
        """
        current = getattr(self, "busy_action", None)
        if current:
            self._log(f"Cannot start {action}: {current} is already in progress.")
            try:
                messagebox.showinfo(
                    "Busy",
                    f"'{current}' is currently running.\n"
                    f"Please wait for it to finish before starting '{action}'."
                )
            except Exception:
                pass
            return False

        self.busy_action = action
        return True

    def _clear_busy(self, action: str) -> None:
        """
        UI-thread only. Clears busy state only if it matches the current action.
        """
        if getattr(self, "busy_action", None) == action:
            self.busy_action = None

    def _begin_action(self, name: str, total: int):
        self.cancel_event.clear()
        self.progress_var.set(f"{name}: 0 / {total}")
        if self.btn_cancel is not None:
            self.btn_cancel.configure(state="normal")

        # Hard cancel only makes sense for Run (subprocess tree kill)
        if getattr(self, "btn_hard_cancel", None) is not None:
            self.btn_hard_cancel.configure(state="normal" if name == "Run" else "disabled")

    def _update_progress(self, name: str, done: int, total: int):
        self.progress_var.set(f"{name}: {done} / {total}")

    def _end_action(self, name: str, cancelled: bool = False):
        if cancelled:
            self.progress_var.set(f"{name}: cancelled")
        else:
            self.progress_var.set(f"{name}: finished")
        if self.btn_cancel is not None:
            self.btn_cancel.configure(state="disabled")
        if getattr(self, "btn_hard_cancel", None) is not None:
            self.btn_hard_cancel.configure(state="disabled")
        self._clear_busy(name)

    def _cancel_current_action(self):
        self.cancel_event.set()

        # Graceful cancel: do not kill processes; stop scheduling new work where possible.
        if getattr(self, "current_action", None) == "Run":
            self._log(
                "Cancel requested (graceful). No new runs will be scheduled; running processes continue unless Hard Cancel is used.")
        else:
            self._log("Cancel requested. Will stop after current item where possible.")

    def _hard_cancel_now(self):
        """
        Hard cancel: immediately kill active Run subprocess trees (psutil-based).
        Safe no-op if nothing is running.
        """
        try:
            killed = cancel_active_processes()
            self._log(f"HARD CANCEL issued. Killed {killed} running process(es).")
            self._append_action_log("hard_cancel", {"killed": killed})
        except Exception as e:
            self._log(f"HARD CANCEL failed: {e}")

    def _ui_post(self, func, *args, **kwargs) -> None:
        """Thread-safe: enqueue UI work to run on the Tk main thread."""
        try:
            self._ui_queue.put((func, args, kwargs))
        except Exception:
            pass

    def _drain_ui_queue(self) -> None:
        """Runs on Tk thread; executes queued UI callables."""
        processed = 0
        while processed < getattr(self, "_ui_queue_max_per_tick", 200):
            try:
                func, args, kwargs = self._ui_queue.get_nowait()
            except Empty:
                break

            try:
                func(*args, **kwargs)
            except Exception as e:
                try:
                    self._log(f"UI queue task failed: {e}")
                except Exception:
                    pass

            processed += 1

        self.after(50, self._drain_ui_queue)

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_text.see(tk.END)
    def _bump_records_version(self) -> None:
        meta = _read_json_file(RECORDS_META_JSON) or {}
        v = meta.get("records_version", 0)
        try:
            v = int(v)
        except Exception:
            v = 0
        v += 1
        meta["records_version"] = v
        meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
        meta["writer"] = "FeatureManagerGUI"
        atomic_write_text(RECORDS_META_JSON, json.dumps(meta, indent=2), encoding="utf-8")
        self.records_version_loaded = v
        self.records_version_current = v

    def _load_records_meta(self) -> None:
        meta = _read_json_file(RECORDS_META_JSON) or {}
        v = meta.get("records_version", 0)
        try:
            v = int(v)
        except Exception:
            v = 0
        self.records_version_loaded = v
        self.records_version_current = v

    def _check_records_external_change(self) -> None:
        # Always re-arm first so exceptions don't stop the loop
        try:
            self.after(2000, self._check_records_external_change)
        except Exception:
            return

        try:
            # Don't interrupt during sync or while we are writing
            if getattr(self, "sync_in_progress", False) or getattr(self, "_records_saving_now", False):
                return

            mtime = RECORDS_JSON.stat().st_mtime if RECORDS_JSON.exists() else 0.0

            meta = _read_json_file(RECORDS_META_JSON) or {}
            v = meta.get("records_version", 0)
            try:
                v = int(v)
            except Exception:
                v = 0

            changed = False
            if mtime and self._records_mtime_loaded and mtime > self._records_mtime_loaded + 0.0001:
                changed = True
            if v and self.records_version_loaded and v != self.records_version_loaded:
                changed = True

            if changed and not getattr(self, "require_reload_after_sync", False):
                self.require_reload_after_sync = True
                self._log("records.json changed on disk. Click Reload to refresh.")
                self._set_main_buttons_state(enabled=False, only_reload=True)

        except Exception:
            return
    # ---------- observability / diagnostics ----------

    def _append_action_log(self, event: str, data: dict) -> None:
        """
        Append one JSON line to Automator/logs/gui_actions.jsonl.
        Never raises.
        """
        try:
            path = STORAGE.path("gui_actions")
            path.parent.mkdir(parents=True, exist_ok=True)
            clean_data = {}
            for k, v in (data or {}).items():
                if isinstance(v, str):
                    clean_data[k] = redact_sensitive_text(v)
                elif isinstance(v, list):
                    clean_data[k] = [redact_sensitive_text(x) if isinstance(x, str) else x for x in v]
                elif isinstance(v, dict):
                    clean_data[k] = {
                        kk: (redact_sensitive_text(vv) if isinstance(vv, str) else vv)
                        for kk, vv in v.items()
                    }
                else:
                    clean_data[k] = v

            row = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": event,
                **clean_data,
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._prune_gui_actions_if_needed()
        except Exception:
            pass

    def _append_jsonl(self, path: Path, row: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _emit_structured_event(
        self,
        event: str,
        *,
        correlation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        session_dir: Optional[Path] = None,
        **data,
    ) -> None:
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "correlation_id": correlation_id or "",
            "session_id": session_id or "",
            **data,
        }

        clean_row = {}
        for k, v in row.items():
            if isinstance(v, str):
                clean_row[k] = redact_sensitive_text(v)
            else:
                clean_row[k] = v

        self._append_jsonl(STRUCTURED_EVENTS_JSONL, clean_row)
        if session_dir is not None:
            self._append_jsonl(Path(session_dir) / "events.jsonl", clean_row)

    def _tail_lines(self, path: Path, max_lines: int) -> list[str]:
        """
        Best-effort: return last max_lines lines from a text file.
        Chunked read from end to avoid loading large files.
        """
        try:
            if not path.exists() or max_lines <= 0:
                return []
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                chunk = 64 * 1024
                data = b""
                pos = end
                while pos > 0 and data.count(b"\n") <= max_lines:
                    read_size = chunk if pos >= chunk else pos
                    pos -= read_size
                    f.seek(pos, os.SEEK_SET)
                    data = f.read(read_size) + data
                lines = data.splitlines()[-max_lines:]
                return [ln.decode("utf-8", errors="replace") for ln in lines]
        except Exception:
            return []

    def _prune_gui_actions_if_needed(self) -> None:
        """
        Keep gui_actions.jsonl from growing forever.
        If it exceeds GUI_ACTIONS_MAX_MB, rewrite it to only the last
        GUI_ACTIONS_KEEP_LINES lines.
        """
        path = STORAGE.path("gui_actions")
        try:
            if not path.exists():
                return

            size_bytes = path.stat().st_size
            if size_bytes <= int(GUI_ACTIONS_MAX_MB * 1024 * 1024):
                return

            keep = self._tail_lines(path, GUI_ACTIONS_KEEP_LINES)
            text = "\n".join(keep).rstrip("\n")
            if text:
                text += "\n"

            atomic_write_text(path, text, encoding="utf-8")
            self._log(f"Pruned gui_actions.jsonl to last {len(keep)} lines.")
        except Exception as e:
            try:
                self._log(f"gui_actions.jsonl prune failed: {redact_sensitive_text(e)}")
            except Exception:
                pass

    def _rotate_sync_transcripts_if_needed(self) -> None:
        """
        Limit the number of sync transcript artifacts kept under logs/sessions.
        Keeps only the most recent SYNC_TRANSCRIPT_KEEP_FILES session transcript files.
        """
        try:
            if not SESSIONS_ROOT.exists():
                return

            transcripts = sorted(
                [p for p in SESSIONS_ROOT.glob("*/sync_transcript.txt") if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )

            if len(transcripts) <= SYNC_TRANSCRIPT_KEEP_FILES:
                return

            stale = transcripts[SYNC_TRANSCRIPT_KEEP_FILES:]
            deleted = 0
            for path in stale:
                try:
                    path.unlink()
                    deleted += 1
                except Exception:
                    pass

            if deleted:
                self._log(f"Pruned {deleted} old sync session transcript file(s).")
        except Exception as e:
            try:
                self._log(f"sync transcript prune failed: {redact_sensitive_text(e)}")
            except Exception:
                pass

    def _start_session_dir(self, action_name: str, meta: Optional[dict] = None) -> Path:
        """
        Create a per-session directory under logs/sessions for the given action.
        Also writes a small session_meta.json for future observability work.
        """
        SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe_action = re.sub(r"[^A-Za-z0-9_-]+", "_", (action_name or "").strip()).strip("_") or "Action"
        session_dir = SESSIONS_ROOT / f"{stamp}_{safe_action}"
        session_dir.mkdir(parents=True, exist_ok=True)

        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "action": action_name,
        }
        if meta:
            payload.update(meta)

        try:
            atomic_write_text(
                session_dir / "session_meta.json",
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            try:
                self._log(f"Failed to write session metadata: {redact_sensitive_text(e)}")
            except Exception:
                pass

        self._emit_structured_event(
            "session_started",
            correlation_id=payload.get("correlation_id"),
            session_id=payload.get("session_id"),
            session_dir=session_dir,
            action=action_name,
            meta=payload,
        )

        return session_dir

    def _copy_diagnostics_to_clipboard(self) -> None:
        """
        Copy a support-friendly diagnostic bundle to clipboard.
        """
        try:
            import platform
        except Exception:
            platform = None

        try:
            lines = []
            lines.append("=== Feature Manager Diagnostics ===")
            lines.append(f"Timestamp: {datetime.now().isoformat(timespec='seconds')}")
            lines.append(f"APP_ROOT: {APP_ROOT}")
            lines.append(f"Python: {sys.version.splitlines()[0]}")
            if platform:
                try:
                    lines.append(f"OS: {platform.platform()}")
                except Exception:
                    pass

            lines.append("")
            lines.append("=== Paths ===")
            lines.append(f"records.json: {RECORDS_JSON}")
            lines.append(f"records_meta.json: {RECORDS_META_JSON}")
            lines.append(f"run_history.jsonl: {RUN_HISTORY_JSONL}")
            lines.append(f"script_runs_dir (current): {self.script_runs_dir_var.get()}")
            lines.append("")

            lines.append("=== Recent GUI log (last 50 lines) ===")
            try:
                content = self.log_text.get("1.0", tk.END)
                gui_lines = [ln for ln in content.splitlines() if ln.strip()]
                lines.extend(gui_lines[-50:])
            except Exception:
                lines.append("(unable to read GUI log widget)")

            lines.append("")
            lines.append("=== Recent run_history.jsonl (last 50 lines) ===")
            for ln in self._tail_lines(RUN_HISTORY_JSONL, 50):
                lines.append(ln)

            text = redact_sensitive_text("\n".join(lines).strip() + "\n")
            self.clipboard_clear()
            self.clipboard_append(text)

            self._log("Diagnostics copied to clipboard.")
            self._append_action_log("diagnostics_copied", {"lines": len(lines)})

            try:
                messagebox.showinfo("Diagnostics", "Diagnostics copied to clipboard.")
            except Exception:
                pass

        except Exception as e:
            self._log(f"Failed to copy diagnostics: {redact_sensitive_text(e)}")

    def _run_startup_health_check(self) -> None:
        """
        Lightweight read-only startup validation.

        Checks:
        - records.json exists and is valid JSON list
        - configured Script Runs directory exists
        - ClickUp data sync script exists
        - logs directory is writable
        - records_meta.json is readable if present
        - records.json.lock is not currently held by another live process

        Never raises and never modifies operational data.
        """
        checks = []
        warnings = []
        failures = []

        # -------------------------------------------------
        # records.json
        # -------------------------------------------------
        try:
            if not RECORDS_JSON.exists():
                failures.append("records.json is missing")
            else:
                obj = json.loads(RECORDS_JSON.read_text(encoding="utf-8"))
                if not isinstance(obj, list):
                    failures.append("records.json root is not a JSON list")
                else:
                    checks.append(f"records.json OK ({len(obj)} records)")
        except Exception as e:
            failures.append(
                f"records.json unreadable: {redact_sensitive_text(e)}"
            )

        # -------------------------------------------------
        # Script Runs directory
        # -------------------------------------------------
        try:
            script_runs_dir = self._get_script_runs_dir()
            if script_runs_dir.exists() and script_runs_dir.is_dir():
                checks.append(f"Script Runs dir OK: {script_runs_dir}")
            else:
                failures.append(
                    f"Script Runs directory missing: {script_runs_dir}"
                )
        except Exception as e:
            failures.append(
                f"Unable to resolve Script Runs directory: "
                f"{redact_sensitive_text(e)}"
            )

        # -------------------------------------------------
        # records_meta.json
        # -------------------------------------------------
        if RECORDS_META_JSON.exists():
            try:
                meta = json.loads(
                    RECORDS_META_JSON.read_text(encoding="utf-8")
                )
                if isinstance(meta, dict):
                    checks.append("records_meta.json OK")
                else:
                    warnings.append(
                        "records_meta.json exists but is not a JSON object"
                    )
            except Exception as e:
                warnings.append(
                    f"records_meta.json unreadable: "
                    f"{redact_sensitive_text(e)}"
                )
        else:
            warnings.append("records_meta.json does not exist yet")

        # -------------------------------------------------
        # Lock state
        # -------------------------------------------------
        if RECORDS_LOCK.exists():
            try:
                text = RECORDS_LOCK.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                match = re.search(r"(?m)^pid=(\d+)\s*$", text)

                if match:
                    lock_pid = int(match.group(1))

                    if psutil.pid_exists(lock_pid):
                        warnings.append(
                            f"records.json is currently locked by "
                            f"live PID {lock_pid}"
                        )
                    else:
                        warnings.append(
                            f"stale records.json.lock detected "
                            f"(dead PID {lock_pid}); it will be "
                            f"recovered automatically on the next write"
                        )
                else:
                    warnings.append(
                        "records.json.lock exists but its PID "
                        "could not be parsed"
                    )

            except Exception as e:
                warnings.append(
                    f"Unable to inspect records.json.lock: "
                    f"{redact_sensitive_text(e)}"
                )
        else:
            checks.append("records.json lock state OK")

        # -------------------------------------------------
        # Logs directory writability
        # -------------------------------------------------
        try:
            logs_dir = APP_ROOT / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)

            probe = logs_dir / ".startup_healthcheck.tmp"
            probe.write_text(
                datetime.now().isoformat(timespec="seconds"),
                encoding="utf-8",
            )
            probe.unlink()

            checks.append("Logs directory writable")

        except Exception as e:
            failures.append(
                f"Logs directory is not writable: "
                f"{redact_sensitive_text(e)}"
            )

        # -------------------------------------------------
        # Report
        # -------------------------------------------------
        if failures:
            overall = "FAIL"
        elif warnings:
            overall = "WARN"
        else:
            overall = "PASS"

        self._log(
            f"Startup health check: {overall} "
            f"({len(checks)} OK, "
            f"{len(warnings)} warning(s), "
            f"{len(failures)} failure(s))"
        )

        for item in checks:
            self._log(f"  OK: {item}")

        for item in warnings:
            self._log(f"  WARN: {item}")

        for item in failures:
            self._log(f"  FAIL: {item}")

        self._append_action_log(
            "startup_health_check",
            {
                "result": overall,
                "ok_count": len(checks),
                "warning_count": len(warnings),
                "failure_count": len(failures),
                "warnings": warnings,
                "failures": failures,
            },
        )

        self._emit_structured_event(
            "startup_health_check",
            result=overall,
            ok_count=len(checks),
            warning_count=len(warnings),
            failure_count=len(failures),
            warnings=warnings,
            failures=failures,
        )

        if failures:
            try:
                messagebox.showwarning(
                    "Feature Manager health check",
                    "Feature Manager started, but one or more "
                    "operational problems were detected.\n\n"
                    + "\n".join(f"- {item}" for item in failures)
                    + "\n\nSee the GUI log for full details.",
                )
            except Exception:
                pass

    def _reload(self):
        self.require_reload_after_sync = False
        self.selected_features.clear()
        self.last_run_results.clear()
        self._load_data()
        self._load_batch_metadata()
        self._reload_clickup_sync_cache()
        self._refresh_table()
        self._refresh_feature_setup_states()
        self._update_heading_labels()

        if self._clickup_sync_pending_reload:
            today = date.today().isoformat()
            try:
                if self._clickup_sync_completed_date != today:
                    raise RuntimeError(
                        "The successful ClickUp sync belongs to an earlier day."
                    )

                disk_records = json.loads(
                    RECORDS_JSON.read_text(encoding="utf-8")
                )
                if not clickup_reloaded_records_match(
                    self.records, disk_records
                ):
                    raise RuntimeError(
                        "The reloaded records do not match records.json "
                        "by feature number, ClickUp identity, frequency, "
                        "or status."
                    )

                state = {
                    "date": today,
                    "status": "SYNCED",
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                    "records_path": str(RECORDS_JSON),
                }
                atomic_write_text(
                    self.clickup_daily_sync_state_path,
                    json.dumps(state, indent=2),
                    encoding="utf-8",
                )
                self.clickup_daily_sync_state = state
                self._clickup_sync_pending_reload = False
                self._clickup_sync_completed_date = None
            except Exception as exc:
                self.require_reload_after_sync = True
                self._log(
                    "Could not finalize daily ClickUp sync: "
                    + redact_sensitive_text(exc)
                )

        self._update_dataops_sync_display()
        self._log("Reloaded records.")

        if self.require_reload_after_sync:
            self._set_main_buttons_state(
                enabled=False, only_reload=True
            )
        else:
            self._set_main_buttons_state(enabled=True)

    def _set_main_buttons_state(self, *, enabled: bool, only_reload: bool = False) -> None:
        # When only_reload=True, everything except Reload is disabled.
        normal = "normal" if enabled else "disabled"
        disabled = "disabled"

        if self.btn_run: self.btn_run.configure(state=disabled if only_reload else normal)
        if getattr(self, "btn_rerun_failed", None):
            self.btn_rerun_failed.configure(state=disabled if only_reload else normal)
        if self.btn_pack: self.btn_pack.configure(state=disabled if only_reload else normal)
        if self.btn_batch: self.btn_batch.configure(state=disabled if only_reload else normal)
        if self.btn_sync_clickup_tasks:
            self.btn_sync_clickup_tasks.configure(
                state=disabled if only_reload else normal
            )
        if getattr(self, "btn_validate", None):
            self.btn_validate.configure(state=disabled if only_reload else normal)
        if getattr(self, "btn_clean_dirs", None):
            self.btn_clean_dirs.configure(state=disabled if only_reload else normal)
        # Copy zips depends on pack state, but must be disabled during sync / post-sync mode.
        if self.btn_copy_zips:
            if only_reload or not enabled:
                self.btn_copy_zips.configure(state="disabled")
            else:
                self.btn_copy_zips.configure(state="normal" if self.pack_finished_once else "disabled")

        if self.btn_select_all: self.btn_select_all.configure(state=disabled if only_reload else normal)
        if self.btn_select_none: self.btn_select_none.configure(state=disabled if only_reload else normal)
        if self.btn_invert: self.btn_invert.configure(state=disabled if only_reload else normal)

        if self.btn_reload:
            self.btn_reload.configure(state="normal" if (only_reload or enabled) else "disabled")

        if self.btn_cancel:
            # keep Cancel disabled unless a Run/Pack/Batch action explicitly enables it
            self.btn_cancel.configure(state="disabled")

    def _start_sync_clickup_task_ids(self):
        if self.sync_in_progress:
            return

        try:
            sync_command = helper_command(
                SYNC_CLICKUP_TASK_IDS_MODULE
            )
        except RuntimeSettingsError as exc:
            messagebox.showerror(
                "Sync data from clickup",
                str(exc),
            )
            return

        action_name = "Sync ClickUp Task IDs"

        if not self._try_begin_busy(action_name):
            return

        session_id = (
            datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            + "_Sync_ClickUp_Task_IDs"
        )
        correlation_id = session_id
        session_dir = self._start_session_dir(
            action_name,
            {
                "script": SYNC_CLICKUP_TASK_IDS_MODULE,
                "session_id": session_id,
                "correlation_id": correlation_id,
            },
        )

        self.sync_in_progress = True
        self.require_reload_after_sync = False
        self._set_main_buttons_state(enabled=False)

        self._append_action_log(
            "clickup_task_id_sync_started",
            {
                "script": SYNC_CLICKUP_TASK_IDS_MODULE,
                "session_id": session_id,
                "correlation_id": correlation_id,
                "session_dir": str(session_dir),
            },
        )

        modal = tk.Toplevel(self)
        modal.title("Sync data from clickup")
        modal.transient(self)
        modal.grab_set()
        modal.resizable(False, False)
        modal.protocol("WM_DELETE_WINDOW", lambda: None)

        ttk.Label(
            modal,
            text="Syncing ClickUp task IDs, frequency, and status...\nPlease wait.",
        ).pack(
            padx=20,
            pady=16,
        )

        self.sync_modal = modal

        def worker():
            cmd = sync_command

            out_lines = []
            err_lines = []

            def _reader(pipe, prefix: str, store: list[str]):
                try:
                    for line in iter(pipe.readline, ""):
                        if not line:
                            break

                        clean = line.rstrip("\r\n")
                        store.append(clean)
                        self._ui_post(
                            self._log,
                            f"{prefix}{clean}",
                        )

                except Exception as exc:
                    self._ui_post(
                        self._log,
                        f"{prefix}(stream error: {exc})",
                    )

                finally:
                    try:
                        pipe.close()
                    except Exception:
                        pass

            try:
                env = os.environ.copy()
                env[CORRELATION_ID_ENV_VAR] = correlation_id
                env[SESSION_ID_ENV_VAR] = session_id
                env["AUTOMATOR_DATA_ROOT"] = str(APP_ROOT)

                package_root = Path(__file__).resolve().parents[3]
                if (package_root / "src" / "automator").is_dir():
                    existing = env.get("PYTHONPATH", "")
                    env["PYTHONPATH"] = str(package_root) + (
                        os.pathsep + existing if existing else ""
                    )

                process = subprocess.Popen(
                    cmd,
                    cwd=str(APP_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    env=env,
                )

                stdout_thread = threading.Thread(
                    target=_reader,
                    args=(
                        process.stdout,
                        "CLICKUP ID SYNC: ",
                        out_lines,
                    ),
                    daemon=True,
                )

                stderr_thread = threading.Thread(
                    target=_reader,
                    args=(
                        process.stderr,
                        "CLICKUP ID SYNC ERR: ",
                        err_lines,
                    ),
                    daemon=True,
                )

                stdout_thread.start()
                stderr_thread.start()

                returncode = process.wait()

                try:
                    stdout_thread.join(timeout=2.0)
                except Exception:
                    pass

                try:
                    stderr_thread.join(timeout=2.0)
                except Exception:
                    pass

                try:
                    transcript_path = (
                        session_dir / "sync_transcript.txt"
                    )

                    parts = [
                        (
                            "timestamp="
                            + datetime.now().isoformat(
                                timespec="seconds"
                            )
                        ),
                        f"returncode={returncode}",
                        f"session_id={session_id}",
                        f"correlation_id={correlation_id}",
                        "",
                        "=== STDOUT ===",
                        *out_lines,
                        "",
                        "=== STDERR ===",
                        *err_lines,
                    ]

                    atomic_write_text(
                        transcript_path,
                        "\n".join(parts) + "\n",
                        encoding="utf-8",
                    )

                    self._rotate_sync_transcripts_if_needed()

                except Exception as exc:
                    self._ui_post(
                        self._log,
                        (
                            "CLICKUP ID SYNC ERR: "
                            f"failed to write transcript: {exc}"
                        ),
                    )

            except Exception as exc:
                returncode = 1
                self._ui_post(
                    self._log,
                    f"CLICKUP ID SYNC ERR: {exc}",
                )

            self._ui_post(
                self._on_clickup_task_id_sync_finished,
                returncode,
                session_id,
                correlation_id,
                session_dir,
            )

        threading.Thread(
            target=worker,
            daemon=True,
        ).start()

    def _on_clickup_task_id_sync_finished(
        self,
        returncode: int,
        session_id: str,
        correlation_id: str,
        session_dir: Path,
    ):
        modal = self.sync_modal
        self.sync_modal = None

        try:
            if modal is not None:
                try:
                    modal.grab_release()
                except Exception:
                    pass

                try:
                    modal.destroy()
                except Exception:
                    pass
        finally:
            self.sync_modal = None

        self.sync_in_progress = False
        self._clear_busy("Sync ClickUp Task IDs")

        report_path = STORAGE.path("clickup_last_run")

        report = _read_json_file(report_path) or {}
        records_changed = bool(
            report.get("records_json_updated", False)
        )

        valid_success = (
                returncode == 0
                and clickup_daily_sync_report_is_current(
            report,
            session_id,
            date.today().isoformat(),
        )
        )

        if valid_success:
            counts = report.get("counts", {})
            if not isinstance(counts, dict):
                counts = {}

            self._log(
                "ClickUp data sync finished successfully: "
                f"{counts.get('VERIFIED', 0)} verified, "
                f"{counts.get('FOUND', 0)} found, "
                f"{counts.get('NO TASK', 0)} no task, "
                f"{counts.get('AMBIGUOUS', 0)} ambiguous, "
                f"{counts.get('IDENTITY CONFLICT', 0)} conflict(s)."
            )

            self._clickup_sync_pending_reload = True
            self._clickup_sync_completed_date = date.today().isoformat()
            self.require_reload_after_sync = True
            self._log(
                "Click Reload to finish today's ClickUp data sync."
            )
            self._set_main_buttons_state(
                enabled=False,
                only_reload=True,
            )
        else:
            self._clickup_sync_pending_reload = False
            self._clickup_sync_completed_date = None
            self.require_reload_after_sync = False
            self._log(
                "ClickUp data sync failed or its completion report "
                f"could not be verified (exit code {returncode})."
            )
            self._set_main_buttons_state(enabled=True)

        self._update_dataops_sync_display()
        self._append_action_log(
            "clickup_task_id_sync_finished",
            {
                "returncode": returncode,
                "records_json_updated": records_changed,
                "session_id": session_id,
                "correlation_id": correlation_id,
                "session_dir": str(session_dir),
            },
        )

    def _get_script_runs_dir(self) -> Path:
        return Path(self.script_runs_dir_var.get()).expanduser()

    def _get_max_workers(self) -> int:
        try:
            v = int(self.max_workers_var.get())
            return v if v > 0 else 4
        except Exception:
            return 4

    def _get_selected_features(self) -> List[str]:
        return sorted(self.selected_features)

    # ---------- filtering/sorting ----------

    def _apply_filters(self, r: FeatureRecord) -> bool:
        if self.filter_selected.get() == "Yes" and r.feature_number not in self.selected_features:
            return False
        if self.filter_selected.get() == "No" and r.feature_number in self.selected_features:
            return False

        if self.filter_frequency.get() != "All" and r.frequency.lower() != self.filter_frequency.get().lower():
            return False

        if self.filter_status.get() != "All" and (r.status or "").lower() != self.filter_status.get().lower():
            return False
        if (
                self.filter_clickup_status.get() != "All"
                and (r.clickup_status or "").lower()
                != self.filter_clickup_status.get().lower()
        ):
            return False
        last_saved_filter = self.filter_last_status_saved.get()
        if last_saved_filter != "All":
            last_saved = (r.last_status_saved or "").strip()
            if last_saved_filter == "No result":
                if last_saved:
                    return False
            elif last_saved.lower() != last_saved_filter.lower():
                return False

        current_filter = self.filter_last_status_session.get()
        if current_filter != "All":
            rr = self.last_run_results.get(r.feature_number)
            current_status = (rr.status if rr else "").strip()
            if current_filter == "No result":
                if current_status:
                    return False
            elif current_status.lower() != current_filter.lower():
                return False

        notes_query = (self.notes_search_var.get() or "").strip().lower()
        if notes_query and notes_query not in (r.notes or "").lower():
            return False

        feature_query = (self.search_var.get() or "").strip().lower()
        if feature_query and feature_query not in (r.feature_number or "").lower():
            return False

        return True

    def _set_filter_from_heading(self, setter, value: str):
        setter(value)
        self._update_heading_labels()
        self._refresh_table()

    def _clear_all_filters(self):
        self.filter_frequency.set("All")
        self.filter_status.set("All")
        self.filter_clickup_status.set("All")
        self.filter_last_status_saved.set("All")
        self.filter_last_status_session.set("All")
        self.filter_selected.set("All")
        self.notes_search_var.set("")
        # (search stays as-is; if you want search cleared too, tell me)

        self._update_heading_labels()
        self._refresh_table()

    def _sort_key(self, r: FeatureRecord):
        col = getattr(self, "sort_col", "feature")

        if col == "selected":
            return r.feature_number in self.selected_features
        if col == "feature":
            return r.feature_number
        if col == "status":
            return (r.status or "").lower()
        if col == "clickup_status":
            return (r.clickup_status or "").lower()
        if col == "frequency":
            return (r.frequency or "").lower()
        if col == "last_update":
            return r.last_update_dt or datetime.min
        if col == "next_due":
            return r.next_due_dt or datetime.max
        if col == "notes":
            return (r.notes or "").lower()
        if col == "bugs":
            return (r.bugs or "").lower()
        if col == "last_duration":
            return r.last_duration_sec if r.last_duration_sec is not None else float("inf")
        if col == "last_status_saved":
            return (r.last_status_saved or "")
        if col == "last_status_session":
            rr = self.last_run_results.get(r.feature_number)
            return (rr.status or "") if rr else ""
        if col == "run_duration_session":
            rr = self.last_run_results.get(r.feature_number)
            return rr.duration_sec if (rr and rr.duration_sec is not None) else float("inf")
        if col == "sqlite_copied_session":
            rr = self.last_run_results.get(r.feature_number)
            return bool(rr.sqlite_copied) if rr else False
        if col == "work_timer":
            interval = self.active_work_intervals.get(r.feature_number)
            return interval.started_at if interval else datetime.max.replace(tzinfo=timezone.utc)
        if col == "setup_state":
            return self.feature_setup_states.get(r.feature_number, "CHECKING")
        if col == "sqlite_compare":
            return self.sqlite_compare_states.get(r.feature_number, "")
        if col == "person_count":
            return self.batch_metadata.get(r.feature_number, {}).get("person_count", -1)
        if col == "output_class":
            return self.batch_metadata.get(r.feature_number, {}).get("output_class", "")
        if col == "clickup_task":
            return (r.clickup_task_id or "").lower()

        if col in {"attachment_status", "task_sync", "time_sync"}:
            state = self.clickup_sync_state.get(
                r.feature_number,
                {},
            )

            if not isinstance(state, dict):
                return ""

            domain = {
                "attachment_status": "attachment_sync",
                "task_sync": "task_sync",
                "time_sync": "time_sync",
            }[col]

            status = state.get(
                domain,
                {},
            ).get(
                "status",
                "",
            )

            if col == "task_sync":
                return display_task_sync_status(
                    status
                )

            return status

        return r.feature_number

    def _on_heading_click(self, col: str):
        if getattr(self, "require_reload_after_sync", False):
            return

        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            self.sort_reverse = False

        self._update_heading_labels()
        self._refresh_table()

    def _update_heading_labels(self):
        """
        Show sort arrow on active sort column and show filter indicator on filterable columns.
        """
        # active filters
        active = {
            "frequency": self.filter_frequency.get(),
            "status": self.filter_status.get(),
            "clickup_status": self.filter_clickup_status.get(),
            "last_status_saved": self.filter_last_status_saved.get(),
            "last_status_session": self.filter_last_status_session.get(),
            "selected": self.filter_selected.get(),
        }

        for col, base_text in self._base_headings.items():
            text = base_text

            # sort arrow
            if col == getattr(self, "sort_col", None):
                text += " ▲" if not getattr(self, "sort_reverse", False) else " ▼"

            # filter indicator (only for the filterable columns)
            if col in active and active[col] != "All":
                text += f"  ▼ [{active[col]}]"

            self.tree.heading(col, text=text)

            # Update the tree column (#0) heading ("Selected") separately
            sel_text = "Selected"

            # sort arrow on Selected
            if getattr(self, "sort_col", None) == "selected":
                sel_text += " ▲" if not getattr(self, "sort_reverse", False) else " ▼"

            # filter indicator on Selected
            cur = self.filter_selected.get()
            if cur != "All":
                sel_text += f"  ▼ [{cur}]"

            self.tree.heading("#0", text=sel_text)

    def _refresh_table(self):
        self.selected_count_var.set(f"Selected: {len(self.selected_features)}")

        for item in self.tree.get_children():
            self.tree.delete(item)

        # Dedicated per-column search row directly beneath the headings.
        # Feature and Notes Entry widgets are positioned over their cells.
        self.tree.insert(
            "",
            tk.END,
            iid="__notes_filter__",
            values=(
                "",  # feature
                "",  # status
                "",  # clickup_status
                "",  # frequency
                "",  # last_update
                "",  # next_due
                "",  # notes
                "",  # bugs
                "",  # last_duration
                "",  # last_status_saved
                "",  # last_status_session
                "",  # run_duration_session
                "",  # sqlite_copied_session
                "",  # timeout_sec
                "",  # work_timer
                "CHECKING",  # setup_state
                "",  # sqlite_compare
                "",  # person_count
                "",  # output_class
                "",  # clickup_task
                "",  # attachment_status
                "",  # task_sync
                "",  # time_sync
            ),
            tags=("filter_row",),
        )

        filtered = [r for r in self.records if self._apply_filters(r)]
        filtered.sort(key=self._sort_key, reverse=getattr(self, "sort_reverse", False))

        for idx, r in enumerate(filtered):
            checked = r.feature_number in self.selected_features
            img = self.img_checked if checked else self.img_unchecked

            rr = self.last_run_results.get(r.feature_number)
            status_session = rr.status if rr else ""
            duration_session = rr.duration_hms if rr else ""
            sqlite_copied_session = ("Yes" if rr.sqlite_copied else "No") if rr else ""

            if rr and rr.status in {"failure", "timeout"}:
                tag = "failure_even" if idx % 2 == 0 else "failure_odd"
            else:
                tag = "even" if idx % 2 == 0 else "odd"

            self.tree.insert(
                "",
                tk.END,
                iid=r.feature_number,
                image=img,
                values=(
                    r.feature_number,
                    r.status,
                    r.clickup_status,
                    r.frequency,
                    r.last_update_display,
                    r.next_due_display,
                    r.notes,
                    r.bugs,
                    r.last_duration_hms,
                    r.last_status_saved,
                    status_session,
                    duration_session,
                    sqlite_copied_session,
                    (str(r.timeout_sec) if r.timeout_sec else ""),
                    self._work_timer_display(r.feature_number),
                    self.feature_setup_states.get(r.feature_number, "CHECKING"),
                    self.sqlite_compare_states.get(r.feature_number, ""),
                    self.batch_metadata.get(r.feature_number, {}).get("person_count", ""),
                    self.batch_metadata.get(r.feature_number, {}).get("output_class", ""),
                    r.clickup_task_id,
                    self.clickup_sync_state.get(r.feature_number, {}).get("attachment_sync", {}).get("status", ""),
                    display_task_sync_status(
                        self.clickup_sync_state
                        .get(
                            r.feature_number,
                            {},
                        )
                        .get(
                            "task_sync",
                            {},
                        )
                        .get(
                            "status",
                            "",
                        )
                    ),
                    self.clickup_sync_state.get(r.feature_number, {}).get("time_sync", {}).get("status", ""),
                ),
                tags=(tag,),
            )

        self.after_idle(self._position_table_filter_entries)

    def _get_filtered_records(self) -> List[FeatureRecord]:
        filtered = [r for r in self.records if self._apply_filters(r)]
        filtered.sort(key=self._sort_key, reverse=self.sort_reverse)

        return filtered

    def _select_all_filtered(self):
        for r in self._get_filtered_records():
            self.selected_features.add(r.feature_number)
        self._refresh_table()

    def _select_none_filtered(self):
        for r in self._get_filtered_records():
            self.selected_features.discard(r.feature_number)
        self._refresh_table()

    def _invert_selection_filtered(self):
        for r in self._get_filtered_records():
            if r.feature_number in self.selected_features:
                self.selected_features.remove(r.feature_number)
            else:
                self.selected_features.add(r.feature_number)
        self._refresh_table()

    def _on_tree_click(self, event):
        if getattr(self, "require_reload_after_sync", False):
            return

        row_id = self.tree.identify_row(event.y)
        if row_id == "__notes_filter__":
            return
        col = self.tree.identify_column(event.x)
        region = self.tree.identify("region", event.x, event.y)

        if not row_id:
            return

        if region == "tree" and col == "#0":
            if row_id in self.selected_features:
                self.selected_features.remove(row_id)
            else:
                self.selected_features.add(row_id)
            self._refresh_table()

    def _position_table_filter_entries(self):
        """
        Position each search widget over its own logical Treeview column.

        Displayed #n identifiers change when columns are hidden or reordered,
        so resolve each logical column against the Treeview's current
        displaycolumns before asking for its bounding box.
        """
        entries = (
            ("feature", getattr(self, "feature_filter_entry", None)),
            ("notes", getattr(self, "notes_filter_entry", None)),
        )

        try:
            if not self.tree.exists("__notes_filter__"):
                for _column_name, entry in entries:
                    if entry is not None:
                        entry.place_forget()
                return

            columns = self.tree["columns"]
            displaycolumns = self.tree["displaycolumns"]

            for column_name, entry in entries:
                if entry is None:
                    continue

                try:
                    column_id = display_column_id(
                        columns,
                        displaycolumns,
                        column_name,
                    )

                    if column_id is None:
                        entry.place_forget()
                        continue

                    bbox = self.tree.bbox(
                        "__notes_filter__",
                        column_id,
                    )

                    if not bbox:
                        entry.place_forget()
                        continue

                    x, y, width, height = bbox

                    entry.place(
                        x=x + 2,
                        y=y + 2,
                        width=max(40, width - 4),
                        height=max(20, height - 4),
                    )
                    entry.lift()

                except Exception:
                    entry.place_forget()

        except Exception:
            for _column_name, entry in entries:
                if entry is not None:
                    try:
                        entry.place_forget()
                    except Exception:
                        pass

    def _launch_editor_at_location(self, file_path: str, line_no: int) -> tuple[bool, str]:
        """
        Open a file at a specific line using the configured editor.

        Required environment variables:
        - FEATURE_MANAGER_EDITOR      -> "pycharm", "vscode", "code", or "default"
        - FEATURE_MANAGER_EDITOR_CMD  -> executable/launcher command

        Behavior:
        - pycharm: <cmd> --line <n> <file>
        - vscode/code: <cmd> --goto <file>:<n>
        - default: open file with system default app
        """
        file_path = str(file_path)
        line_no = int(line_no)

        editor_type = os.environ.get("FEATURE_MANAGER_EDITOR", "").strip().lower()
        editor_cmd = os.environ.get("FEATURE_MANAGER_EDITOR_CMD", "").strip()

        if not editor_type:
            return False, "Editor is not configured. Run configure_editor_env.py first."

        if editor_type == "default":
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", file_path])
                    return True, "Opened with macOS default app"
                if os.name == "nt":
                    os.startfile(file_path)
                    return True, "Opened with Windows default app"
                subprocess.Popen(["xdg-open", file_path])
                return True, "Opened with system default app"
            except Exception as e:
                return False, f"Default open failed: {e}"

        if not editor_cmd:
            return False, "Editor command is not configured. Run configure_editor_env.py first."

        try:
            if editor_type in {"vscode", "code"}:
                subprocess.Popen([editor_cmd, "--goto", f"{file_path}:{line_no}"])
                return True, "VS Code launched"
            if editor_type == "pycharm":
                subprocess.Popen([editor_cmd, "--line", str(line_no), file_path])
                return True, "PyCharm launched"
            return False, f"Unsupported editor type: {editor_type}"
        except Exception as e:
            return False, f"Editor launch failed: {e}"

    def _open_failed_feature_location(self, row_id: str) -> None:
        res = self.last_run_results.get(row_id)
        if not res or res.status not in {"failure", "timeout"}:
            return

        error_file_path = getattr(res, "error_file_path", None)
        error_line_no = getattr(res, "error_line_no", None)
        error_source_line = getattr(res, "error_source_line", None)

        if error_file_path and error_line_no is not None:
            location = f"{error_file_path}:{error_line_no}"

            try:
                self.clipboard_clear()
                self.clipboard_append(location)
            except Exception:
                pass

            opened, launch_msg = self._launch_editor_at_location(error_file_path, error_line_no)
            if not opened:
                self._log(f"feature_{res.feature}: {launch_msg}")

            detail = f"Failure location copied to clipboard:\n{location}"
            if error_source_line:
                detail += f"\n\nCode:\n{redact_sensitive_text(error_source_line)}"

            try:
                messagebox.showinfo("Failure location", redact_sensitive_text(detail))
            except Exception:
                pass
            return

        if res.stderr:
            try:
                self.clipboard_clear()
                self.clipboard_append(redact_sensitive_text(res.stderr))
            except Exception:
                pass
            self._log(f"feature_{res.feature}: precise file:line not found; redacted traceback copied to clipboard.")

    def _on_tree_double_click(self, event):
        row_id = self.tree.identify_row(event.y)
        if not row_id or row_id == "__notes_filter__":
            return
        self._open_failed_feature_location(row_id)

    def _on_tree_heading_right_click(self, event):
        if getattr(self, "require_reload_after_sync", False):
            return

        # Only handle right-clicks on headings
        region = self.tree.identify_region(event.x, event.y)
        if region != "heading":
            return

        col_id = self.tree.identify_column(event.x)  # e.g. '#1', '#2', ...
        if not col_id:
            return

        # Handle the "Selected" tree column (#0) BEFORE index parsing
        if col_id == "#0":
            menu = tk.Menu(self, tearoff=0)

            current = self.filter_selected.get()
            for opt in ["All", "Yes", "No"]:
                label = f"✓ {opt}" if opt == current else opt
                menu.add_command(
                    label=label,
                    command=lambda v=opt: self._set_filter_from_heading(self.filter_selected.set, v),
                )

            menu.add_separator()
            menu.add_command(label="Clear all filters", command=self._clear_all_filters)
            menu.tk_popup(event.x_root, event.y_root)
            return

        col = display_column_name(
            self.tree["columns"],
            self.tree["displaycolumns"],
            col_id,
        )

        if col is None:
            return

        # Only these columns have heading-filters
        if col not in {
            "frequency",
            "status",
            "clickup_status",
            "last_status_saved",
            "last_status_session",
        }:
            return

        menu = tk.Menu(self, tearoff=0)

        # Build options depending on column type
        run_status_options = [
            "All",
            "success",
            "failure",
            "timeout",
            "skipped",
            "No result",
        ]

        if col == "status":
            options = ["All"] + STATUS_OPTIONS
            current = self.filter_status.get()
            setter = self.filter_status.set

        elif col == "clickup_status":
            options = [
                "All",
                "Complete",
                "In Progress",
                "To do",
                "To be checked",
            ]
            current = self.filter_clickup_status.get()
            setter = self.filter_clickup_status.set

        elif col == "frequency":
            options = [
                "All",
                "Daily",
                "Weekly",
                "Monthly",
                "Quarterly",
                "Biannually",
                "Annually",
            ]
            current = self.filter_frequency.get()
            setter = self.filter_frequency.set

        elif col == "last_status_saved":
            options = run_status_options
            current = self.filter_last_status_saved.get()
            setter = self.filter_last_status_saved.set

        elif col == "last_status_session":
            options = run_status_options
            current = self.filter_last_status_session.get()
            setter = self.filter_last_status_session.set
        else:
            options = ["All"]
            current = "All"
            setter = lambda *_: None

        # Add menu items for the clicked column
        for opt in options:
            label = f"✓ {opt}" if opt == current else opt
            menu.add_command(
                label=label,
                command=lambda v=opt, s=setter: self._set_filter_from_heading(s, v),
            )

        # Add Flags submenu (hidden columns, still filterable)

        menu.add_separator()
        menu.add_command(label="Clear all filters", command=self._clear_all_filters)

        # show menu at cursor
        menu.tk_popup(event.x_root, event.y_root)

    # ---------- actions ----------
    def _rerun_failed(self):
        if not self._require_daily_clickup_sync("Rerun failed"):
            return
        failed_features = sorted(
            feature
            for feature, result in self.last_run_results.items()
            if result and result.status in {"failure", "timeout"}
        )

        if not failed_features:
            messagebox.showinfo(
                "Rerun failed",
                "No failed or timed-out runs from the current session."
            )
            return

        # Replace the current selection with exactly the failed/timed-out features.
        self.selected_features = set(failed_features)
        self._refresh_table()

        self._log(
            f"Rerun failed: selected {len(failed_features)} "
            f"failed/timed-out feature(s)."
        )
        self._append_action_log(
            "rerun_failed_requested",
            {
                "count": len(failed_features),
                "features": failed_features,
            },
        )

        # Reuse the normal run pipeline so timeout handling, logging,
        # CSV output, cancellation, persistence, etc. remain identical.
        self._start_run_selected()

    def _start_run_selected(self):
        if not self._require_daily_clickup_sync("Run selected"):
            return
        features = self._get_selected_features()
        if not features:
            messagebox.showinfo("Run", "No features selected.")
            return

        script_runs_dir = self._get_script_runs_dir()
        if not script_runs_dir.exists():
            messagebox.showerror("Run", f"Script Runs directory does not exist:\n{script_runs_dir}")
            return
        if not self._try_begin_busy("Run"):
            return

        max_workers = self._get_max_workers()
        session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "_Run"
        correlation_id = session_id
        session_dir = self._start_session_dir(
            "Run",
            {
                "selected_count": len(features),
                "max_workers": max_workers,
                "session_id": session_id,
                "correlation_id": correlation_id,
            },
        )

        self._log(f"Running {len(features)} features (max_workers={max_workers})...")
        self._append_action_log(
            "run_started",
            {
                "selected_count": len(features),
                "max_workers": max_workers,
                "session_id": session_id,
                "correlation_id": correlation_id,
                "session_dir": str(session_dir),
            },
        )
        self._emit_structured_event(
            "run_started",
            correlation_id=correlation_id,
            session_id=session_id,
            session_dir=session_dir,
            selected_count=len(features),
            max_workers=max_workers,
        )

        total = len(features)
        self._begin_action("Run", total)
        self.current_action = "Run"

        def worker():
            if self.cancel_event.is_set():
                self.current_action = None
                self._ui_post(self._end_action, "Run", cancelled=True)
                self._ui_post(self._log, "Run cancelled before start.")
                self._append_action_log(
                    "run_finished",
                    {
                        "selected_count": len(features),
                        "cancelled": True,
                        "session_id": session_id,
                        "correlation_id": correlation_id,
                        "session_dir": str(session_dir),
                    },
                )
                self._emit_structured_event(
                    "run_finished",
                    correlation_id=correlation_id,
                    session_id=session_id,
                    session_dir=session_dir,
                    selected_count=len(features),
                    cancelled=True,
                )
                return

            done = 0

            def progress(res: RunResult):
                nonlocal done
                if self.cancel_event.is_set():
                    return
                done += 1
                self._ui_post(self._handle_run_result, res)
                self._ui_post(self._update_progress, "Run", done, total)

            workflow_status_map = {}
            for f in features:
                rec = self.feature_map.get(str(f))
                workflow_status_map[str(f)] = rec.status if rec else ""

            feature_metadata_map = {}
            for f in features:
                rec = self.feature_map.get(str(f))
                feature_metadata_map[str(f)] = {
                    "frequency": rec.frequency if rec else "",
                    "bugs": rec.bugs if rec else "",
                    "notes": rec.notes if rec else "",
                }

            feature_timeout_map = {}
            for f in features:
                rec = self.feature_map.get(str(f))
                feature_timeout_map[str(f)] = int(rec.timeout_sec) if (rec and rec.timeout_sec) else None

            try:
                run_features(
                    features=features,
                    max_workers=max_workers,
                    script_runs_dir=script_runs_dir,
                    logs_dir=session_dir,
                    progress_callback=progress,
                    history_path=RUN_HISTORY_JSONL,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    workflow_status_map=workflow_status_map,
                    feature_metadata_map=feature_metadata_map,
                    feature_timeout_map=feature_timeout_map,
                    cancel_event=self.cancel_event,
                    runtime_settings=self.runtime_settings,
                )
            except TypeError:
                run_features(
                    features=features,
                    max_workers=max_workers,
                    script_runs_dir=script_runs_dir,
                    logs_dir=session_dir,
                    progress_callback=progress,
                    history_path=RUN_HISTORY_JSONL,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    workflow_status_map=workflow_status_map,
                    cancel_event=self.cancel_event,
                    runtime_settings=self.runtime_settings,
                )

            self.current_action = None
            self._ui_post(self._end_action, "Run", cancelled=self.cancel_event.is_set())
            self._ui_post(
                self._log,
                f"{'Run cancelled.' if self.cancel_event.is_set() else 'Run finished.'} Session dir: {session_dir}",
            )

            self._append_action_log(
                "run_finished",
                {
                    "selected_count": len(features),
                    "cancelled": bool(self.cancel_event.is_set()),
                    "session_id": session_id,
                    "correlation_id": correlation_id,
                    "session_dir": str(session_dir),
                },
            )
            self._emit_structured_event(
                "run_finished",
                correlation_id=correlation_id,
                session_id=session_id,
                session_dir=session_dir,
                selected_count=len(features),
                cancelled=bool(self.cancel_event.is_set()),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _handle_run_result(self, res: RunResult):
        self.last_run_results[res.feature] = res

        rec = self.feature_map.get(res.feature)
        if rec:
            # Persist last_status for record-keeping (always)
            rec.last_status_saved = (res.status or "").strip().lower()

            # A successful run creates a new candidate and
            # invalidates the previous approval/sync cycle.
            if res.status == "success":
                if res.timestamp:
                    rec.last_update_dt = (
                        res.timestamp
                    )
                    rec.last_update_str = (
                        res.timestamp.isoformat(
                            timespec="seconds"
                        )
                    )
                    rec.next_due_dt = (
                        compute_next_due(
                            rec.frequency,
                            rec.last_update_dt,
                        )
                    )
                    rec.last_duration_sec = (
                        res.duration_sec
                    )

                self.sqlite_compare_states.pop(
                    res.feature,
                    None,
                )

                invalidate_after_successful_run(
                    self.clickup_state,
                    res.feature,
                    rec.last_update_str,
                )

                self._reload_clickup_sync_cache()

        self._request_save_records()

        timeout_used = rec.timeout_sec if rec else None

        if res.status in {"failure", "timeout"} and getattr(res, "error_file_path", None) and getattr(
                res,
                "error_line_no",
                None,
        ) is not None:
            short_name = Path(res.error_file_path).name
            final_error = redact_sensitive_text(
                res.stderr.splitlines()[-1].strip()
            ) if res.stderr else redact_sensitive_text(res.message)
            self._log(
                f"feature_{res.feature}: {res.status.upper()} at {short_name}:{res.error_line_no} "
                f"(duration={res.duration_hms or 'N/A'}, sqlite_copied={res.sqlite_copied}, timeout_sec={timeout_used}) - {final_error}"
            )
            if getattr(res, "error_source_line", None):
                self._log(f"    {redact_sensitive_text(res.error_source_line)}")
        else:
            self._log(
                f"feature_{res.feature}: {res.status.upper()} "
                f"(duration={res.duration_hms or 'N/A'}, sqlite_copied={res.sqlite_copied}, timeout_sec={timeout_used}) - {res.message}"
            )

        self._emit_structured_event(
            "run_result",
            correlation_id=getattr(res, "correlation_id", None),
            session_id=getattr(res, "session_id", None),
            session_dir=getattr(res, "session_dir", None),
            feature_number=res.feature,
            workflow_status=getattr(res, "workflow_status", "") or "",
            result=res.status,
            duration_sec=res.duration_sec,
            duration_hms=res.duration_hms,
            sqlite_copied=bool(res.sqlite_copied),
            timeout_sec_used=timeout_used,
            message=res.message or "",
            error_file_path=getattr(res, "error_file_path", None),
            error_line_no=getattr(res, "error_line_no", None),
        )

        self._refresh_table()

    # ---------- manual work tracking ----------

    def _schedule_feature_setup_refresh(self, *_):
        if self._close_requested:
            return
        if self._setup_refresh_after is not None:
            try:
                self.after_cancel(self._setup_refresh_after)
            except Exception:
                pass
        self._setup_refresh_after = self.after(500, self._refresh_feature_setup_states)

    def _canonical_outputs_dir(self) -> Path:
        return canonical_paths(RUNTIME_SETTINGS.data_root, self._get_script_runs_dir()).outputs


    def _update_dataops_sync_display(self):
        """Show standalone mode without requiring external services."""
        self.dataops_sync_status_var.set("Mode: Standalone / Offline")

    def _require_daily_clickup_sync(self, action):
        """Legacy compatibility: local workflows never require ClickUp."""
        return True


    def _create_clickup_client(self):
        from dotenv import load_dotenv
        load_dotenv(APP_ROOT / ".env")
        token = os.environ.get("API_KEY", "").strip()
        list_id = os.environ.get("CLICKUP_LIST_ID", "").strip()
        return ClickUpClient(token, list_id)

    def _restore_canonical_approvals(self):
        restored = 0

        for feature, record in self.feature_map.items():
            canonical = (
                    self._canonical_outputs_dir()
                    / (feature + ".sqlite")
            )

            approved, _reason = (
                canonical_approval_is_current(
                    self.clickup_state,
                    feature,
                    canonical,
                    record.last_update_str,
                )
            )

            if approved:
                self.sqlite_compare_states[
                    feature
                ] = "SAME"
                restored += 1

        if restored:
            self._log(
                "Restored %d persisted SQLite approval(s)."
                % restored
            )

    def _attach_selected_outputs(self):
        if not self._require_daily_clickup_sync(
                "Sync selected"
        ):
            return

        features = self._get_selected_features()

        if not features:
            messagebox.showinfo(
                "Sync selected",
                "Select one or more features first.",
            )
            return

        eligible = []
        rejected = []

        for feature in features:
            record = self.feature_map.get(
                feature
            )

            if record is None:
                rejected.append(
                    "feature_%s: feature record is missing"
                    % feature
                )
                continue

            canonical = (
                    self._canonical_outputs_dir()
                    / (feature + ".sqlite")
            )

            try:
                item, reason = build_workflow_item(
                    self.clickup_state,
                    feature_number=feature,
                    last_status=record.last_status_saved,
                    frequency=record.frequency,
                    last_update_dt=record.last_update_dt,
                    last_update_iso=record.last_update_str,
                    task_id=record.clickup_task_id,
                    canonical_path=canonical,
                )

            except Exception as exc:
                item = None
                reason = redact_sensitive_text(
                    exc
                )

            if item is None:
                rejected.append(
                    "feature_%s: %s"
                    % (
                        feature,
                        reason,
                    )
                )
                continue

            eligible.append(
                item
            )

        if not eligible:
            messagebox.showwarning(
                "Sync selected",
                (
                        "No selected feature is eligible "
                        "for ClickUp sync.\n\n"
                        + "\n".join(rejected)
                ),
            )
            return

        prompt = (
                     "Run the approved ClickUp workflow for "
                     "%d feature(s)?\n\n"
                     "For each eligible feature Feature Manager will:\n"
                     "1. set the permanent task to Complete to fire recurrence;\n"
                     "2. verify that the same task returns to In Progress;\n"
                     "3. upload and verify the approved canonical SQLite;\n"
                     "4. update Last Update, Start and Due dates;\n"
                     "5. inspect the ClickUp WorkflowStatus field;\n"
                      "6. only if ClickUp WorkflowStatus is Analyzed, "
                     "change it to Prepare for upload in ClickUp "
                     "and verify the updated custom field.\n\n"
                     "All other WorkflowStatus values are left unchanged.\n"
                     "No ClickUp task will be created or reassigned."
                 ) % len(eligible)

        if rejected:
            prompt += (
                    "\n\nThe following selected feature(s) "
                    "will be skipped:\n"
                    + "\n".join(rejected)
            )

        if not messagebox.askyesno(
                "Sync selected",
                prompt,
                icon="warning",
        ):
            return

        def worker():
            client = self._create_clickup_client()

            return sync_clickup_features(
                client,
                self.clickup_state,
                eligible,
            )

        def completed(
                outcomes,
                error,
        ):
            if error:
                messagebox.showerror(
                    "Sync selected",
                    redact_sensitive_text(
                        error
                    ),
                )
                return

            self._reload_clickup_sync_cache()

            summary = []
            local_status_changed = False

            for item in outcomes:
                suffix = (
                    " - "
                    + redact_sensitive_text(
                        item.error
                    )
                    if item.error
                    else ""
                )

                if item.dataops_ok:
                    record = self.feature_map.get(
                        item.feature_number
                    )

                    if record is not None:
                        record.status = (
                            "Prepare for upload"
                        )
                        local_status_changed = True

                summary.append(
                    "feature_%s: %s%s"
                    % (
                        item.feature_number,
                        item.status,
                        suffix,
                    )
                )

                self._log(
                    "feature_%s ClickUp workflow: %s%s"
                    % (
                        item.feature_number,
                        item.status,
                        suffix,
                    )
                )

                self._append_action_log(
                    "clickup_feature_workflow",
                    {
                        "feature_number": (
                            item.feature_number
                        ),
                        "status": item.status,
                        "clickup_task_id": (
                            item.clickup_task_id
                        ),
                        "old_attachment_remains": (
                            item.old_attachment_remains
                        ),
                        "dataops_ok": (
                            item.dataops_ok
                        ),
                        "error": (
                            redact_sensitive_text(
                                item.error
                            )
                        ),
                    },
                )

            if local_status_changed:
                self._request_save_records()

            self._refresh_table()

            messagebox.showinfo(
                "ClickUp sync results",
                "\n".join(summary),
            )

        self._submit_work_job(
            "Sync selected ClickUp workflow",
            worker,
            completed,
        )

    def _reload_clickup_sync_cache(self):
        self.clickup_sync_state = self.clickup_state.read()

    def _run_sqlite_comparison_demo(self):
        def prepared(demo, error):
            if error:
                messagebox.showerror('SQLite Comparison Demo', str(error), parent=self)
                return
            initial = demo.initial_result
            prompt = (
                    'Synthetic SQLite comparison: ' + initial.state + '\n\n'
                    + (initial.message or '')
                    + '\n\nAccept the synthetic candidate as the new canonical? '
                      'This archives the previous synthetic canonical in the isolated demo folder.'
            )
            if not messagebox.askyesno('SQLite Comparison Demo', prompt, parent=self):
                messagebox.showinfo('SQLite Comparison Demo',
                                    'Candidate not accepted. Both SQLite files remain in:\n'
                                    + str(demo.run_directory), parent=self)
                return

            def accepted(result, failure):
                if failure:
                    messagebox.showerror('SQLite Comparison Demo', str(failure), parent=self)
                    return
                messagebox.showinfo('SQLite Comparison Demo',
                                    'Accepted. Repeat comparison: ' + result.state
                                    + '\nHistory, canonical, and candidate saved to:\n'
                                    + str(demo.run_directory), parent=self)

            self._submit_work_job('Accept synthetic SQLite canonical',
                                  lambda: accept_comparison_demo(demo), accepted)

        self._submit_work_job('Prepare synthetic SQLite comparison',
                              lambda: prepare_comparison_demo(RUNTIME_SETTINGS.data_root),
                              prepared)

    def _compare_selected_features(self):
        if not self._require_daily_clickup_sync("Compare selected"):
            return
        features = self._get_selected_features()
        if not features:
            messagebox.showinfo("Compare selected", "Select one or more features first.")
            return
        script_runs_dir = self._get_script_runs_dir()
        canonical_dir = self._canonical_outputs_dir()
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_Compare_SQLite"
        session_dir = self._start_session_dir(
            "Compare SQLite",
            {"features": features, "canonical_dir": str(canonical_dir), "session_id": session_id, "correlation_id": session_id},
        )

        def completed(results, error):
            if error:
                messagebox.showerror("Compare selected", redact_sensitive_text(error))
                return
            for result in results:
                self.sqlite_compare_states[result.feature_number] = result.state
                record = self.feature_map.get(
                    result.feature_number
                )

                if (
                    result.state == "SAME"
                    and result.canonical.is_file()
                    and record is not None
                ):
                    try:
                        mark_canonical_approved(
                            self.clickup_state,
                            result.feature_number,
                            result.canonical,
                            record.last_update_str,
                            source="SAME",
                            digest=result.canonical_sha256,
                        )

                    except Exception as exc:
                        self._log(
                            "feature_%s SQLite approval persistence failed: %s"
                            % (
                                result.feature_number,
                                redact_sensitive_text(exc),
                            )
                        )

                elif result.state in {
                    "DIFFERENT",
                    "NO CANONICAL",
                }:
                    mark_canonical_review_required(
                        self.clickup_state,
                        result.feature_number,
                        result.state,
                    )
                self._log("feature_%s SQLite comparison: %s%s" % (
                    result.feature_number, result.state,
                    (" - " + result.message) if result.message else "",
                ))
                self._append_action_log("sqlite_comparison", {
                    "feature_number": result.feature_number,
                    "state": result.state,
                    "candidate": str(result.candidate),
                    "canonical": str(result.canonical),
                    "candidate_sha256": result.candidate_sha256,
                    "canonical_sha256": result.canonical_sha256,
                    "candidate_logical_sha256": result.candidate_logical_sha256,
                    "canonical_logical_sha256": result.canonical_logical_sha256,
                    "byte_identical": result.byte_identical,
                    "message": result.message,
                    "session_id": session_id,
                    "correlation_id": session_id,
                    "session_dir": str(session_dir),
                })
            self._refresh_table()
            self._show_comparison_review(results)

        self._submit_work_job(
            "Compare selected SQLite files",
            lambda: compare_features(features, script_runs_dir, canonical_dir),
            completed,
        )

    def _show_comparison_review(self, results):
        window = tk.Toplevel(self)
        window.title("SQLite Comparison Review")
        self._maximize_window(window)
        window.resizable(True, True)
        columns = ("feature", "state", "candidate", "canonical", "message")
        table = ttk.Treeview(window, columns=columns, show="headings", selectmode="browse")
        for name, title, width in (
            ("feature", "Feature", 90), ("state", "Comparison", 145),
            ("candidate", "Candidate", 260), ("canonical", "Canonical", 260), ("message", "Details", 260),
        ):
            table.heading(name, text=title)
            table.column(name, width=width, anchor=tk.W)
        table.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        details_frame = ttk.LabelFrame(
            window,
            text="Selected comparison",
        )

        details_frame.pack(
            side=tk.BOTTOM,
            fill=tk.X,
            padx=8,
            pady=(4, 8),
            before=table,
        )

        details_body = ttk.Frame(
            details_frame,
        )

        details_body.pack(
            fill=tk.BOTH,
            expand=True,
            padx=6,
            pady=6,
        )

        details_scroll = ttk.Scrollbar(
            details_body,
            orient=tk.VERTICAL,
        )

        details_scroll.pack(
            side=tk.RIGHT,
            fill=tk.Y,
        )

        details_text = tk.Text(
            details_body,
            height=7,
            wrap="word",
            state=tk.DISABLED,
            yscrollcommand=details_scroll.set,
        )

        details_text.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
        )

        details_scroll.configure(
            command=details_text.yview,
        )
        by_feature = {
            item.feature_number: item
            for item in results
        }

        for item in results:
            summary = getattr(
                item,
                "summary",
                None,
            )

            display_state = (
                summary.tier
                if summary is not None
                else item.state
            )

            message_preview = (
                item.message.splitlines()[0]
                if item.message
                else ""
            )

            table.insert(
                "",
                tk.END,
                iid=item.feature_number,
                values=(
                    item.feature_number,
                    display_state,
                    str(item.candidate),
                    str(item.canonical),
                    message_preview,
                ),
            )

        def selected_result():
            chosen = table.selection()
            return by_feature.get(chosen[0]) if chosen else None
        def show_selected(_event=None):
            item = selected_result()

            details_text.configure(
                state=tk.NORMAL
            )

            details_text.delete(
                "1.0",
                tk.END,
            )

            if item is None:
                details_text.insert(
                    tk.END,
                    "Select a comparison result to view its details.",
                )

            else:
                details = (
                    item.message
                    or (
                        "Comparison state: %s"
                        % item.state
                    )
                )

                details_text.insert(
                    tk.END,
                    details,
                )

            details_text.configure(
                state=tk.DISABLED
            )

        table.bind(
            "<<TreeviewSelect>>",
            show_selected,
        )

        def open_path(path):
            if not path.is_file():
                messagebox.showwarning("Open SQLite", "File does not exist:\n" + str(path), parent=window)
                return
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", str(path)])
                elif os.name == "nt":
                    os.startfile(str(path))
                else:
                    subprocess.Popen(["xdg-open", str(path)])
            except Exception as exc:
                messagebox.showerror("Open SQLite", redact_sensitive_text(exc), parent=window)

        def open_candidate():
            item = selected_result()
            if item:
                open_path(item.candidate)

        def open_canonical():
            item = selected_result()
            if item:
                open_path(item.canonical)

        def open_both():
            item = selected_result()
            if item:
                open_path(item.candidate)
                open_path(item.canonical)

        def inspect_differences():
            item = selected_result()
            if not item or item.state not in {"SAME", "DIFFERENT"}:
                messagebox.showinfo("SQLite Differences", "Select a completed comparison first.", parent=window)
                return

            def inspected(report, error):
                if error:
                    messagebox.showerror("SQLite Differences", redact_sensitive_text(error), parent=window)
                    return
                if window.winfo_exists():
                    self._show_sqlite_difference_details(item.feature_number, report, parent=window)

            self._submit_work_job(
                "Inspect SQLite differences",
                lambda: inspect_sqlite_differences(item.candidate, item.canonical),
                inspected,
            )

        def accept_candidate():
            item = selected_result()
            if not item or item.state not in {"DIFFERENT", "NO CANONICAL"}:
                messagebox.showinfo("Accept Candidate", "Select a DIFFERENT or NO CANONICAL result first.", parent=window)
                return
            prompt = "Replace the canonical SQLite with this candidate?\n\n" + str(item.canonical)
            if not messagebox.askyesno("Accept Candidate", prompt, icon="warning", parent=window):
                return

            def accepted(digest, error):
                if error:
                    messagebox.showerror("Accept Candidate", redact_sensitive_text(error), parent=window)
                    return
                record = self.feature_map.get(
                    item.feature_number
                )

                if record is None:
                    messagebox.showerror(
                        "Accept Candidate",
                        "Feature record is missing.",
                        parent=window,
                    )
                    return

                mark_canonical_approved(
                    self.clickup_state,
                    item.feature_number,
                    item.canonical,
                    record.last_update_str,
                    source="ACCEPTED",
                    digest=digest,
                )
                self.sqlite_compare_states[item.feature_number] = "SAME"
                self._refresh_table()
                table.set(item.feature_number, "state", "SAME")
                self._log("feature_%s candidate accepted as canonical (%s)." % (item.feature_number, digest))
                self._append_action_log("canonical_candidate_accepted", {
                    "feature_number": item.feature_number, "candidate": str(item.candidate),
                    "canonical": str(item.canonical), "sha256": digest,
                })

            self._submit_work_job(
                "Accept SQLite candidate",
                lambda: safe_replace_from_candidate(item.candidate, item.canonical),
                accepted,
            )

        controls = ttk.Frame(window)
        controls.pack( side=tk.TOP, fill=tk.X, padx=8, pady=(8, 4), before=table, )
        for label, command in (("Inspect stored differences", inspect_differences),
                               ("Open Candidate", open_candidate), ("Open Canonical", open_canonical),
                               ("Open Both", open_both), ("Accept Candidate / As Canonical", accept_candidate)):
            ttk.Button(controls, text=label, command=command).pack(side=tk.LEFT, padx=4)
        ttk.Button(controls, text="Close", command=window.destroy).pack(side=tk.RIGHT, padx=4)
        if results:
            table.selection_set(results[0].feature_number)
            show_selected()

    def _show_sqlite_difference_details(self, feature_number: str, report: SQLiteDifferenceReport, parent):
        window = tk.Toplevel(self)
        window.title("SQLite Differences: Feature " + feature_number)
        self._maximize_window(window)
        window.resizable(True, True)
        ttk.Label(
            window,
            text=("Read-only stored-value inspection. This view includes values ignored by the logical comparison. "
                  "Up to 10 examples per table; text limit: 240 characters; BLOB preview limit: 240 bytes."),
        ).pack(anchor=tk.W, padx=10, pady=(10, 4))
        columns = ("table", "state", "candidate_rows", "canonical_rows", "primary_key", "summary")
        table = ttk.Treeview(window, columns=columns, show="headings", selectmode="browse", height=12)
        headings = {
            "table": "Table", "state": "State", "candidate_rows": "Candidate rows",
            "canonical_rows": "Canonical rows", "primary_key": "Primary key", "summary": "Summary",
        }
        widths = {
            "table": 180, "state": 145, "candidate_rows": 110, "canonical_rows": 110,
            "primary_key": 160, "summary": 440,
        }
        for column in columns:
            table.heading(column, text=headings[column])
            table.column(column, width=widths[column], anchor=tk.W)
        table.pack(fill=tk.X, padx=10, pady=(0, 8))
        by_table = {item.table_name: item for item in report.table_differences}
        for item in report.table_differences:
            table.insert(
                "", tk.END, iid=item.table_name,
                values=(item.table_name, item.state,
                        "" if item.candidate_rows is None else item.candidate_rows,
                        "" if item.canonical_rows is None else item.canonical_rows,
                        ", ".join(item.primary_key), item.message),
            )

        details = tk.Text(window, wrap="word", height=16, state=tk.DISABLED)
        details.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        def show_selected(_event=None):
            chosen = table.selection()
            if not chosen:
                return
            item = by_table[chosen[0]]
            lines = ["Table: %s" % item.table_name, "State: %s" % item.state]
            if item.message:
                lines.append("Summary: %s" % item.message)
            if item.primary_key:
                lines.append("Primary key: %s" % ", ".join(item.primary_key))
            if item.examples:
                lines.append("")
                lines.append("Examples:")
                for number, example in enumerate(item.examples, start=1):
                    lines.append("%d. %s" % (number, example["kind"].capitalize()))
                    lines.append("   Key: %s" % json.dumps(example.get("key", {}), ensure_ascii=False, sort_keys=True))
                    if "columns" in example:
                        for column, values in example["columns"].items():
                            lines.append("   %s: canonical=%r | candidate=%r" % (
                                column, values["canonical"], values["candidate"],
                            ))
                    if "canonical" in example:
                        lines.append("   Canonical row: %s" % json.dumps(example["canonical"], ensure_ascii=False, sort_keys=True))
                    if "candidate" in example:
                        lines.append("   Candidate row: %s" % json.dumps(example["candidate"], ensure_ascii=False, sort_keys=True))
            else:
                lines.append("")
                lines.append("No row examples are available for this table.")
            details.configure(state=tk.NORMAL)
            details.delete("1.0", tk.END)
            details.insert("1.0", "\n".join(lines))
            details.configure(state=tk.DISABLED)

        table.bind("<<TreeviewSelect>>", show_selected)
        ttk.Button(window, text="Close", command=window.destroy).pack(anchor=tk.E, padx=10, pady=(0, 10))
        if report.table_differences:
            table.selection_set(report.table_differences[0].table_name)
            show_selected()

    def _refresh_feature_setup_states(self):
        self._setup_refresh_after = None
        if self._close_requested:
            return
        features = [record.feature_number for record in self.records]
        script_runs_dir = self._get_script_runs_dir()
        for feature in features:
            self.feature_setup_states[feature] = "CHECKING"
        self._refresh_table()

        def done(result, error):
            if error:
                self._log("Could not inspect feature setup: " + redact_sensitive_text(error))
                for feature in features:
                    self.feature_setup_states[feature] = "ERROR"
            else:
                self.feature_setup_states.update({feature: inspection.state for feature, inspection in result.items()})
            self._refresh_table()

        self._submit_work_job(
            "Inspect feature setups",
            lambda: inspect_feature_setups(features, script_runs_dir),
            done,
        )

    def _initialize_selected_features(self):
        if not self._require_daily_clickup_sync("Initialize selected"):
            return
        features = self._get_selected_features()
        if not features:
            messagebox.showinfo("Initialize features", "Select one or more features first.")
            return
        script_runs_dir = self._get_script_runs_dir()

        def initialized(outcomes, error):
            if error:
                messagebox.showerror("Initialize features", redact_sensitive_text(error))
                return
            messages = []
            for feature, inspection, item_error in outcomes:
                if item_error:
                    self.feature_setup_states[feature] = "PARTIAL SETUP"
                    messages.append("feature_%s: %s" % (feature, redact_sensitive_text(item_error)))
                else:
                    self.feature_setup_states[feature] = inspection.state
            self._refresh_table()
            if messages:
                messagebox.showwarning("Initialization results", "Some features need attention:\n\n" + "\n".join(messages))
            else:
                self._log("Initialized or verified %d feature(s)." % len(outcomes))

        def inspected(result, error):
            if error:
                messagebox.showerror("Initialize features", redact_sensitive_text(error))
                return
            partial = [item.feature_number for item in result.values() if item.state == "PARTIAL SETUP"]
            fill_missing = False
            if partial:
                detail = "\n".join("feature_" + item for item in partial)
                if not messagebox.askyesno(
                    "Complete partial setup?",
                    "Add only missing template files to these feature folders? Existing files will be kept.\n\n" + detail,
                ):
                    self.feature_setup_states.update({key: item.state for key, item in result.items()})
                    self._refresh_table()
                    return
                fill_missing = True
            self._submit_work_job(
                "Initialize selected features",
                lambda: initialize_features(features, script_runs_dir, fill_missing=fill_missing),
                initialized,
            )

        self._submit_work_job(
            "Inspect selected feature setups",
            lambda: inspect_feature_setups(features, script_runs_dir),
            inspected,
        )

    def _start_development_selected(self):
        if not self._require_daily_clickup_sync("Start Development"):
            return
        features = self._get_selected_features()
        if not features:
            messagebox.showinfo("Start Development", "Select one or more features first.")
            return
        script_runs_dir = self._get_script_runs_dir()

        def inspected(result, error):
            if error:
                messagebox.showerror("Start Development", redact_sensitive_text(error))
                return
            partial = [item.feature_number for item in result.values() if item.state == "PARTIAL SETUP"]
            fill_missing = False
            if partial:
                detail = "\n".join("feature_" + item for item in partial)
                if not messagebox.askyesno(
                    "Complete partial setup?",
                    "Add only missing template files before starting development? Existing files will be kept.\n\n" + detail,
                ):
                    self.feature_setup_states.update({key: item.state for key, item in result.items()})
                    self._refresh_table()
                    return
                fill_missing = True

            def prepared(result, error):
                if error:
                    messagebox.showerror("Start Development", redact_sensitive_text(error))
                    return
                outcomes, successful = result
                failures = []
                for feature, inspection, item_error in outcomes:
                    if item_error:
                        self.feature_setup_states[feature] = "PARTIAL SETUP"
                        failures.append("feature_%s: %s" % (feature, redact_sensitive_text(item_error)))
                    else:
                        self.feature_setup_states[feature] = inspection.state
                self._refresh_table()
                if successful:
                    self._start_work_features(successful, "development")
                if failures:
                    messagebox.showwarning(
                        "Start Development",
                        "Development timers were not started for features with setup errors:\n\n" + "\n".join(failures),
                    )

            def prepare():
                outcomes = initialize_features(features, script_runs_dir, fill_missing=fill_missing)
                successful = [feature for feature, inspection, item_error in outcomes if item_error is None and inspection.state == "READY"]
                return outcomes, successful

            self._submit_work_job("Prepare development features", prepare, prepared)

        self._submit_work_job(
            "Inspect development feature setups",
            lambda: inspect_feature_setups(features, script_runs_dir),
            inspected,
        )

    def _initialize_work_ledger(self) -> None:
        def worker():
            ledger = WorkLedger(WORK_LEDGER_DB)
            return ledger, ledger.active_intervals(), ledger.list_categories()

        self._submit_work_job("Initialize work ledger", worker, self._on_work_ledger_ready)

    def _submit_work_job(self, label, worker, callback):
        if self._close_requested:
            return
        self._work_jobs_pending += 1

        def run():
            try:
                result = worker()
                error = None
            except Exception as exc:
                result = None
                error = exc
            self._ui_post(self._finish_work_job, label, callback, result, error)

        threading.Thread(target=run, daemon=True, name="WorkLedger-" + re.sub(r"[^A-Za-z0-9]+", "", label)).start()

    def _finish_work_job(self, label, callback, result, error):
        self._work_jobs_pending = max(0, self._work_jobs_pending - 1)
        try:
            callback(result, error)
        except Exception as exc:
            self._log("Work tracking callback failed: " + redact_sensitive_text(exc))
        finally:
            if self._close_requested and self._work_jobs_pending == 0:
                self._finish_close()

    def _on_work_ledger_ready(self, result, error):
        if self._close_requested:
            return
        if error is not None:
            self.work_ledger_error = redact_sensitive_text(error)
            self.work_status_var.set("Work tracking: unavailable")
            self._log("Work ledger could not open: " + self.work_ledger_error)
            messagebox.showwarning(
                "Work tracking unavailable",
                "Feature Manager could not open the local work ledger. "
                "Timer controls are disabled. Existing feature and run data remain available.\n\n"
                + self.work_ledger_error,
            )
            return

        self.work_ledger, active, self.work_categories = result
        self.active_work_intervals = {item.feature_number: item for item in active}
        self.work_ledger_ready = True
        for index in (0, 1, 4, 6, 7):
            self.work_menu.entryconfigure(index, state="normal")
        self.clickup_menu.entryconfigure(2, state="normal")
        self.clickup_menu.entryconfigure(3, state="normal")
        self._log("Work ledger ready. Recovered active timers: %d." % len(active))
        self._append_action_log(
            "work_ledger_ready",
            {"active_count": len(active), "database": str(WORK_LEDGER_DB)},
        )
        self._refresh_table()
        self._refresh_work_timer_tick()

    @staticmethod
    def _format_work_duration(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return "%02d:%02d:%02d" % (hours, minutes, secs)

    def _refresh_active_work_cache(self, callback=None):
        def done(active, error):
            if error:
                self._log("Could not refresh active work timers: " + redact_sensitive_text(error))
                return
            self.active_work_intervals = {item.feature_number: item for item in active}
            self._refresh_table()
            if callback:
                callback()

        self._submit_work_job("Refresh active work timers", self.work_ledger.active_intervals, done)

    def _work_timer_display(self, feature):
        interval = self.active_work_intervals.get(feature)
        if interval is None:
            return ""
        elapsed = (datetime.now(timezone.utc) - interval.started_at).total_seconds()
        return self._format_work_duration(elapsed)

    def _refresh_work_timer_tick(self):
        if self._close_requested:
            return
        try:
            if self.tree.winfo_exists():
                for record in self.records:
                    if self.tree.exists(record.feature_number):
                        self.tree.set(record.feature_number, "work_timer", self._work_timer_display(record.feature_number))
            active_count = len(self.active_work_intervals)
            if self.work_ledger_ready:
                self.work_status_var.set("Work tracking: ready | active timers: %d" % active_count)
            self.after(1000, self._refresh_work_timer_tick)
        except Exception:
            return

    def _choose_work_category(self, callback):
        if not self.work_ledger_ready:
            return
        window = tk.Toplevel(self)
        window.title("Start Work")
        window.transient(self)
        window.grab_set()
        window.resizable(False, False)
        category_var = tk.StringVar(value="development" if "development" in self.work_categories else "work")
        ttk.Label(window, text="Work category:").grid(row=0, column=0, padx=10, pady=10, sticky=tk.W)
        choice = ttk.Combobox(window, textvariable=category_var, values=self.work_categories, state="readonly", width=24)
        choice.grid(row=0, column=1, padx=10, pady=10)

        def finish(accepted):
            value = category_var.get().strip()
            try:
                window.grab_release()
            except Exception:
                pass
            window.destroy()
            if accepted and value:
                callback(value)

        buttons = ttk.Frame(window)
        buttons.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 10), sticky=tk.E)
        ttk.Button(buttons, text="Cancel", command=lambda: finish(False)).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(buttons, text="Start", command=lambda: finish(True)).pack(side=tk.RIGHT)
        choice.focus_set()
        window.bind("<Return>", lambda _event: finish(True))
        window.bind("<Escape>", lambda _event: finish(False))

    def _start_work_selected(self):
        if not self._require_daily_clickup_sync("Start Work"):
            return
        features = self._get_selected_features()
        if not features:
            messagebox.showinfo("Start Work", "Select one or more features first.")
            return
        self._choose_work_category(lambda category: self._start_work_features(features, category))

    def _start_work_features(self, features, category):
        script_runs_dir = self._get_script_runs_dir()
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_Start_Work"
        session_dir = self._start_session_dir(
            "Start Work",
            {"features": features, "category": category, "session_id": session_id, "correlation_id": session_id},
        )

        def worker():
            outcomes = []
            for feature in features:
                try:
                    interval = self.work_ledger.start(
                        feature,
                        category=category,
                        session_id=session_id,
                        correlation_id=session_id,
                    )
                    source = script_runs_dir / ("feature_" + feature) / "main.py"
                    if source.is_file():
                        opened, message = self._launch_editor_at_location(str(source), 1)
                    else:
                        opened, message = False, "main.py does not exist at " + str(source)
                    outcomes.append((feature, interval, opened, message, None))
                except Exception as exc:
                    outcomes.append((feature, None, False, "", exc))
            return outcomes

        self._submit_work_job("Start Work", worker, lambda result, error: self._on_work_started(result, error, session_id, session_dir))

    def _on_work_started(self, outcomes, error, session_id, session_dir):
        if error is not None:
            self._log("Start Work failed: " + redact_sensitive_text(error))
            return
        warnings = []
        for feature, interval, opened, message, error in outcomes:
            if error is not None:
                self._log("feature_%s: timer did not start: %s" % (feature, redact_sensitive_text(error)))
                continue
            self.active_work_intervals[feature] = interval
            self._log("feature_%s: work timer started (%s)." % (feature, interval.id))
            self._append_action_log(
                "work_timer_started",
                {"feature_number": feature, "interval_id": interval.id, "category": interval.category,
                 "session_id": session_id, "correlation_id": session_id},
            )
            self._emit_structured_event(
                "work_timer_started", session_id=session_id, correlation_id=session_id,
                session_dir=session_dir, feature_number=feature, interval_id=interval.id,
                category=interval.category,
            )
            if not opened:
                warnings.append("feature_%s: %s" % (feature, message))
                self._log("feature_%s: timer remains active; editor did not open: %s" % (feature, message))
        self._refresh_table()
        if warnings and not self._close_requested:
            messagebox.showwarning("Editor could not open", "Work timers remain active.\n\n" + "\n".join(warnings))

    def _stop_work_selected(self):
        active_features = [feature for feature in self._get_selected_features() if feature in self.active_work_intervals]
        if not active_features:
            messagebox.showinfo("Stop Work", "No selected feature has an active work timer.")
            return
        if not messagebox.askyesno("Stop Work", "Stop the active work timer for %d selected feature(s)?" % len(active_features)):
            return
        self._stop_work_features(active_features)

    def _stop_work_features(self, features, *, interval_ids=None, on_complete=None):
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_Stop_Work"
        session_dir = self._start_session_dir("Stop Work", {"features": features, "session_id": session_id, "correlation_id": session_id})
        chosen_ids = interval_ids or {}
        active_snapshot = dict(self.active_work_intervals)

        def worker():
            outcomes = []
            for feature in features:
                interval = active_snapshot.get(feature)
                if interval is None:
                    outcomes.append((feature, None, LedgerError("No active timer.")))
                    continue
                try:
                    result = self.work_ledger.stop_interval(chosen_ids.get(feature, interval.id))
                    outcomes.append((feature, result, None))
                except Exception as exc:
                    outcomes.append((feature, None, exc))
            return outcomes

        def complete(result, error):
            if error is not None:
                self._log("Stop Work failed: " + redact_sensitive_text(error))
            else:
                for feature, interval, failure in result:
                    if failure:
                        self._log("feature_%s: timer did not stop: %s" % (feature, redact_sensitive_text(failure)))
                        continue
                    self.active_work_intervals.pop(feature, None)
                    duration = (interval.ended_at - interval.started_at).total_seconds()
                    self._log("feature_%s: work timer stopped (%s, %s)." % (feature, interval.id, self._format_work_duration(duration)))
                    self._append_action_log(
                        "work_timer_stopped",
                        {"feature_number": feature, "interval_id": interval.id, "started_at": interval.started_at.isoformat(),
                         "ended_at": interval.ended_at.isoformat(), "duration_sec": duration,
                         "session_id": session_id, "correlation_id": session_id},
                    )
                    self._emit_structured_event(
                        "work_timer_stopped", session_id=session_id, correlation_id=session_id,
                        session_dir=session_dir, feature_number=feature, interval_id=interval.id,
                        started_at=interval.started_at.isoformat(), ended_at=interval.ended_at.isoformat(),
                        duration_sec=duration,
                    )
            self._refresh_table()
            if on_complete:
                on_complete()

        self._submit_work_job("Stop Work", worker, complete)

    def _manage_work_categories(self):
        if not self.work_ledger_ready:
            return
        window = tk.Toplevel(self)
        window.title("Work Categories")
        window.transient(self)
        self._maximize_window(window)
        listing = tk.Listbox(window, height=12, exportselection=False)
        listing.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        category_var = tk.StringVar()
        ttk.Entry(window, textvariable=category_var).pack(fill=tk.X, padx=10)
        status_var = tk.StringVar(value="")
        ttk.Label(window, textvariable=status_var).pack(fill=tk.X, padx=10, pady=4)
        buttons = ttk.Frame(window)
        buttons.pack(fill=tk.X, padx=10, pady=10)

        def refresh():
            def done(result, error):
                if error:
                    status_var.set(redact_sensitive_text(error))
                    return
                self.work_categories = result
                listing.delete(0, tk.END)
                for name in result:
                    listing.insert(tk.END, name)
            self._submit_work_job("Load work categories", self.work_ledger.list_categories, done)

        def add_category():
            name = category_var.get().strip()
            if not name:
                status_var.set("Enter a category name.")
                return
            def done(_result, error):
                status_var.set(redact_sensitive_text(error) if error else "Category saved.")
                category_var.set("")
                refresh()
            self._submit_work_job("Add work category", lambda: self.work_ledger.add_category(name), done)

        def retire_category():
            selection = listing.curselection()
            if not selection:
                status_var.set("Select a category to retire.")
                return
            name = listing.get(selection[0])
            if not messagebox.askyesno("Retire category", "Retire '%s'? Existing intervals will keep this category." % name, parent=window):
                return
            def done(_result, error):
                status_var.set(redact_sensitive_text(error) if error else "Category retired.")
                refresh()
            self._submit_work_job("Retire work category", lambda: self.work_ledger.retire_category(name), done)

        ttk.Button(buttons, text="Add category", command=add_category).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Retire selected", command=retire_category).pack(side=tk.LEFT, padx=8)
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side=tk.RIGHT)
        refresh()

    @staticmethod
    def _format_interval_time(value):
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S") if value else ""

    @staticmethod
    def _parse_interval_time(value, label, *, required):
        from dateutil import tz

        text = value.strip()
        if not text and not required:
            return None
        if not text:
            raise WorkLedgerValidationError(label + " is required.")
        try:
            local_value = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz.tzlocal())
        except ValueError as exc:
            raise WorkLedgerValidationError(label + " must use YYYY-MM-DD HH:MM:SS.") from exc
        if not tz.datetime_exists(local_value):
            raise WorkLedgerValidationError(label + " is not a valid local time.")
        if tz.datetime_ambiguous(local_value):
            raise WorkLedgerValidationError(label + " is ambiguous because of a daylight-saving transition.")
        return local_value

    def _show_weekly_reconciliation(self):
        if not self.work_ledger_ready:
            messagebox.showwarning("Weekly reconciliation", "The local work ledger is not ready.")
            return
        path = STORAGE.path("reconciliation")
        try:
            saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError, TypeError):
            saved = {}
        end_day = date.today()
        begin_day = end_day - timedelta(days=6)
        closed_days = []
        for value, row in saved.items():
            if not isinstance(row, dict) or row.get("status") != "CLOSED":
                continue
            try:
                saved_day = date.fromisoformat(value)
            except (TypeError, ValueError):
                continue
            if begin_day <= saved_day <= end_day and self.work_ledger.is_day_closed(saved_day):
                closed_days.append((saved_day, row))
        closed_days.sort(key=lambda item: item[0])

        def worker():
            intervals = []
            for day, _row in closed_days:
                day_start = datetime.combine(day, datetime_time.min, self.work_ledger.billing_timezone).astimezone(timezone.utc)
                day_end = datetime.combine(day + timedelta(days=1), datetime_time.min, self.work_ledger.billing_timezone).astimezone(timezone.utc)
                for interval in self.work_ledger.list_intervals(day=day):
                    if interval.ended_at is None:
                        continue
                    start = max(interval.started_at, day_start)
                    end = min(interval.ended_at, day_end)
                    if end > start:
                        intervals.append((day, interval, (end - start).total_seconds()))
            return intervals

        def done(intervals, error):
            if error:
                messagebox.showerror("Weekly reconciliation", redact_sensitive_text(error))
                return
            window = tk.Toplevel(self)
            window.title("Weekly Reconciliation — closed days")
            self._maximize_window(window)
            rows = {}
            for _day, interval, seconds in intervals:
                key = (interval.feature_number, interval.category)
                total = rows.setdefault(key, [0.0, 0.0])
                total[0] += seconds
                if interval.sync_status == "synced":
                    total[1] += seconds
            recorded = sum(value[0] for value in rows.values())
            submitted = sum(value[1] for value in rows.values())
            overlap = sum(float(row.get("overlap_seconds", 0) or 0) for _, row in closed_days)
            covered = sum(float(row.get("covered_seconds", 0) or 0) for _, row in closed_days)
            wall_clock = sum(float(row.get("wall_clock_seconds", 0) or 0) for _, row in closed_days)
            untracked = sum(float(row.get("potential_untracked_seconds", 0) or 0) for _, row in closed_days)
            ttk.Label(
                window,
                text="%s to %s | Closed days: %d | Wall-clock: %s | Local tracked: %s | ClickUp submitted: %s | Parallel overlap: %s | Covered: %s | Potential untracked: %s" % (
                    begin_day.isoformat(), end_day.isoformat(), len(closed_days),
                    self._format_work_duration(wall_clock),
                    self._format_work_duration(recorded), self._format_work_duration(submitted),
                    self._format_work_duration(overlap), self._format_work_duration(covered),
                    self._format_work_duration(untracked),
                ),
            ).pack(fill=tk.X, padx=10, pady=10)
            columns = ("feature", "category", "local", "clickup")
            listing = ttk.Treeview(window, columns=columns, show="headings")
            for name, title, width in (("feature", "Feature", 120), ("category", "Category", 180),
                                       ("local", "Local Time", 180), ("clickup", "ClickUp Time", 180)):
                listing.heading(name, text=title)
                listing.column(name, width=width, anchor=tk.W)
            listing.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
            for (feature, category), (local_seconds, clickup_seconds) in sorted(rows.items()):
                listing.insert("", tk.END, values=(
                    feature, category, self._format_work_duration(local_seconds),
                    self._format_work_duration(clickup_seconds),
                ))
            ttk.Button(window, text="Close", command=window.destroy).pack(anchor=tk.E, padx=10, pady=10)

        self._submit_work_job("Load weekly reconciliation", worker, done)

    def _start_end_workday_review(self):
        if not self.work_ledger_ready:
            messagebox.showwarning("End Workday", "The local work ledger is not ready.")
            return
        day = datetime.now().date()

        def loaded(result, error):
            if error:
                messagebox.showerror("End Workday", redact_sensitive_text(error))
                return
            active, intervals, totals = result
            if active:
                details = "\n".join(
                    "feature_%s  %s" % (item.feature_number, self._work_timer_display(item.feature_number))
                    for item in active
                )
                messagebox.showwarning(
                    "Cannot finalize workday",
                    "Stop or correct all active timers before finalizing.\n\n" + details,
                )
                return
            self._show_end_workday_review(day, intervals, totals)

        def worker():
            active = self.work_ledger.active_intervals()
            intervals = self.work_ledger.list_intervals(day=day)
            totals = self.work_ledger.daily_totals(day)
            return active, intervals, totals

        self._submit_work_job("Load end-of-day review", worker, loaded)

    def _show_end_workday_review(self, day, intervals, totals):
        window = tk.Toplevel(self)
        window.title("End Workday / Sync ClickUp — %s" % day.isoformat())
        self._maximize_window(window)
        window.transient(self)
        completed = [item for item in intervals if item.ended_at is not None]
        day_start = datetime.combine(day, datetime_time.min, self.work_ledger.billing_timezone).astimezone(timezone.utc)
        day_end = datetime.combine(day + timedelta(days=1), datetime_time.min, self.work_ledger.billing_timezone).astimezone(timezone.utc)
        clipped = [(max(item.started_at, day_start), min(item.ended_at, day_end)) for item in completed]
        clipped = [(start, end) for start, end in clipped if end > start]
        wall_elapsed = max(0.0, (max((end for _start, end in clipped), default=day_start)
                                 - min((start for start, _end in clipped), default=day_start)).total_seconds())
        potential_untracked = max(0.0, wall_elapsed - totals.covered_seconds)
        summary = ttk.Label(
            window,
            text="First start → last stop: %s = %s    Tracked time: %s    Parallel overlap: %s    Potential untracked: %s" % (
                self._format_interval_time(min((item.started_at for item in completed), default=None)) or "—",
                self._format_work_duration(wall_elapsed),
                self._format_work_duration(totals.recorded_seconds),
                self._format_work_duration(totals.overlap_seconds),
                self._format_work_duration(potential_untracked),
            ),
        )
        summary.pack(fill=tk.X, padx=10, pady=8)
        columns = ("feature", "category", "started", "ended", "duration", "compare", "attachment", "task_sync", "time_sync", "notes")
        listing = ttk.Treeview(window, columns=columns, show="headings", selectmode="extended")
        for name, title, width in (
            ("feature", "Feature", 90), ("category", "Category", 110),
            ("started", "Started", 130), ("ended", "Ended", 130),
            ("duration", "Duration", 85), ("compare", "Compare", 105),
            ("attachment", "Attachment", 145), ("task_sync", "Task Sync", 95),
            ("time_sync", "Time Sync", 95), ("notes", "Notes", 180),
        ):
            listing.heading(name, text=title)
            listing.column(name, width=width, anchor=tk.W)
        listing.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        interval_map = {}
        for interval in intervals:
            end = interval.ended_at
            duration = ((end - interval.started_at).total_seconds()) if end else 0
            sync_state = self.clickup_sync_state.get(interval.feature_number, {})
            listing.insert("", tk.END, iid=interval.id, values=(
                interval.feature_number, interval.category,
                self._format_interval_time(interval.started_at),
                self._format_interval_time(end) if end else "ACTIVE",
                self._format_work_duration(duration),
                self.sqlite_compare_states.get(interval.feature_number, ""),
                sync_state.get("attachment_sync", {}).get("status", ""),
                display_task_sync_status(sync_state.get("task_sync",{},).get("status","",)),
                interval.sync_status.upper(), interval.notes,
            ))
            interval_map[interval.id] = interval
            if end is not None and interval.sync_status != "synced":
                listing.selection_add(interval.id)

        status_var = tk.StringVar(value="Review local intervals. ClickUp writes occur only after an explicit sync action.")
        ttk.Label(window, textvariable=status_var, anchor=tk.W).pack(fill=tk.X, padx=10, pady=4)

        def refresh_intervals():
            self._start_end_workday_review()
            window.destroy()

        def selected_intervals():
            return [interval_map[item] for item in listing.selection() if item in interval_map]

        def eod_action_is_busy():
            if self._work_jobs_pending:
                messagebox.showinfo(
                    "End-of-day action running",
                    "Wait for the current background operation to finish before starting another end-of-day action.",
                    parent=window,
                )
                return True
            return False

        def review_ledger():
            self._open_work_ledger()

        def sync_times():
            if eod_action_is_busy():
                return
            chosen = selected_intervals()
            if not chosen:
                messagebox.showinfo("Time sync", "Select completed intervals to sync.", parent=window)
                return
            chosen_ids = [item.id for item in chosen if item.ended_at is not None and item.sync_status != "synced"]
            if not chosen_ids:
                messagebox.showinfo("Time sync", "Selected intervals are already synced or still active.", parent=window)
                return
            if not messagebox.askyesno(
                "Sync reviewed time entries",
                "Submit %d selected completed interval(s) to ClickUp? Each local interval remains a separate entry." % len(chosen_ids),
                parent=window,
            ):
                return
            status_var.set("Submitting reviewed intervals…")
            self._sync_work_intervals(chosen_ids, day, lambda message: (status_var.set(message), refresh_intervals()))

        def release_uncertain_times():
            if eod_action_is_busy():
                return
            chosen = [item for item in selected_intervals()
                      if item.sync_status in {"unknown", "in_flight", "reconciling"}]
            if not chosen:
                messagebox.showinfo("Reconcile time entries", "Select an unresolved time entry first.", parent=window)
                return
            prompt = (
                "For each selected interval, confirm that no matching interval marker is visible in ClickUp "
                "and no earlier ClickUp request is still processing. Releasing it permits a later sync attempt. "
                "This can create a duplicate if the original request is still pending or hidden."
            )
            if not messagebox.askyesno("Release after manual verification", prompt, parent=window):
                return
            ids = [item.id for item in chosen]
            self._submit_work_job(
                "Release manually reconciled time entries",
                lambda: [self.work_ledger.release_sync_for_manual_reconciliation(
                    interval_id, confirmed_no_request_pending=True) for interval_id in ids],
                lambda _result, error: (messagebox.showerror("Reconcile time entries", redact_sensitive_text(error), parent=window)
                                        if error else refresh_intervals()),
            )

        def sync_tasks():
            if eod_action_is_busy():
                return
            features = sorted({item.feature_number for item in intervals if item.ended_at is not None})
            if not features:
                messagebox.showinfo("Task sync", "No completed local intervals exist for this day.", parent=window)
                return
            if not messagebox.askyesno(
                "Sync task updates",
                "Update existing ClickUp task dates from local records for %d worked feature(s)? Descriptions remain unchanged." % len(features),
                parent=window,
            ):
                return
            status_var.set("Updating existing ClickUp tasks…")
            self._sync_clickup_task_updates(features, lambda message: (status_var.set(message), refresh_intervals()))

        def sync_attachments():
            if eod_action_is_busy():
                return
            features = sorted({item.feature_number for item in intervals if item.ended_at is not None})
            eligible = [feature for feature in features if self.sqlite_compare_states.get(feature) == "SAME"]
            if not eligible:
                messagebox.showinfo("Attachment sync", "No worked feature has an accepted SAME canonical SQLite.", parent=window)
                return
            if not messagebox.askyesno(
                "Sync accepted SQLite attachments",
                "Upload the accepted canonical SQLite for %d worked feature(s)?" % len(eligible), parent=window,
            ):
                return
            status_var.set("Uploading accepted SQLite attachments…")
            self._sync_clickup_attachments(eligible, lambda message: (status_var.set(message), refresh_intervals()))

        def release_uncertain_attachments():
            if eod_action_is_busy():
                return
            features = sorted({item.feature_number for item in selected_intervals()})
            current_state = self.clickup_state.read()
            attempts = []
            for feature in features:
                domain = current_state.get(feature, {}).get("attachment_sync", {})
                if isinstance(domain, dict) and domain.get("status") in {"UPLOAD IN FLIGHT", "UPLOAD UNKNOWN"}:
                    attempts.append((feature, domain.get("attempt_id")))
            if not attempts:
                messagebox.showinfo("Reconcile attachments", "Select a feature with an unresolved attachment upload.", parent=window)
                return
            prompt = (
                "For each selected feature, confirm that the target ClickUp task has no matching accepted SQLite "
                "attachment and no upload request is still processing. Releasing permits another upload. "
                "This can create a duplicate if the earlier upload is still pending or hidden."
            )
            if not messagebox.askyesno("Release after manual verification", prompt, parent=window):
                return

            def release():
                return [self.clickup_state.release_uncertain_attachment(
                    feature, attempt_id=attempt_id, confirmed_no_attachment=True,
                ) for feature, attempt_id in attempts]

            def released(_result, error):
                if error:
                    messagebox.showerror("Reconcile attachments", redact_sensitive_text(error), parent=window)
                else:
                    self._reload_clickup_sync_cache()
                    refresh_intervals()

            self._submit_work_job("Release manually reconciled attachments", release, released)

        def close_day():
            if eod_action_is_busy():
                return
            status_var.set("Checking active timers and latest ledger state…")

            def current_state():
                return self.work_ledger.active_intervals(), self.work_ledger.list_intervals(day=day), self.work_ledger.daily_totals(day)

            def checked(state, error):
                if error:
                    messagebox.showerror("Close workday", redact_sensitive_text(error), parent=window)
                    return
                active, current_intervals, current_totals = state
                if active:
                    self._refresh_active_work_cache()
                    messagebox.showwarning(
                        "Cannot close day",
                        "Active timers remain:\n\n" + "\n".join(
                            "feature_%s  %s" % (item.feature_number, self._work_timer_display(item.feature_number))
                            for item in active
                        ),
                        parent=window,
                    )
                    return
                self._finalize_workday_close(window, day, current_intervals, current_totals, status_var)

            self._submit_work_job("Check workday close state", current_state, checked)

        actions = ttk.Frame(window)
        actions.pack(fill=tk.X, padx=10, pady=8)
        ttk.Button(actions, text="Review / edit intervals", command=review_ledger).pack(side=tk.LEFT)
        ttk.Label(
            actions,
            text="Task/attachment sync: use ClickUp → Sync selected",
        ).pack(
            side=tk.LEFT,
            padx=5,
        )
        ttk.Button(actions, text="Release uncertain attachments", command=release_uncertain_attachments).pack(side=tk.LEFT, padx=5)
        ttk.Button(actions, text="Sync selected time entries", command=sync_times).pack(side=tk.LEFT, padx=5)
        ttk.Button(actions, text="Release after manual verification", command=release_uncertain_times).pack(side=tk.LEFT, padx=5)
        ttk.Button(actions, text="Close day", command=close_day).pack(side=tk.LEFT, padx=5)
        ttk.Button(actions, text="Close review", command=window.destroy).pack(side=tk.RIGHT)

    def _finalize_workday_close(self, window, day, intervals, totals, status_var):
        blockers = []
        worked = sorted({item.feature_number for item in intervals if item.ended_at is not None})
        pending = [item for item in intervals if item.ended_at is not None and item.sync_status != "synced"]
        if pending:
            blockers.append("%d completed interval(s) are not synced." % len(pending))
        for feature in worked:
            row = self.clickup_sync_state.get(feature, {})
            if row.get("task_sync", {}).get("status") != "SYNCED":
                blockers.append("feature_%s task updates are not synced." % feature)
            if self.sqlite_compare_states.get(feature) == "SAME":
                attachment_status = row.get("attachment_sync", {}).get("status")
                if attachment_status not in {"CURRENT", "OLD ATTACHMENT REMAINS"}:
                    blockers.append("feature_%s accepted SQLite attachment is not verified." % feature)
        if blockers:
            day_path = STORAGE.path("reconciliation")
            try:
                try:
                    saved = json.loads(day_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    saved = {}
                saved[day.isoformat()] = {
                    "status": "NEEDS REVIEW", "blockers": blockers,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                atomic_write_text(day_path, json.dumps(saved, indent=2), encoding="utf-8")
            except Exception as exc:
                self._log("Could not persist workday review state: " + redact_sensitive_text(exc))
            messagebox.showwarning("Day needs review", "\n".join(blockers), parent=window)
            return
        day_path = STORAGE.path("reconciliation")
        try:
            try:
                saved = json.loads(day_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
            completed = [item for item in intervals if item.ended_at is not None]
            day_start = datetime.combine(day, datetime_time.min, self.work_ledger.billing_timezone).astimezone(timezone.utc)
            day_end = datetime.combine(day + timedelta(days=1), datetime_time.min, self.work_ledger.billing_timezone).astimezone(timezone.utc)
            wall_start = min((max(item.started_at, day_start) for item in completed), default=None)
            wall_end = max((min(item.ended_at, day_end) for item in completed), default=None)
            wall_elapsed = max(0.0, (wall_end - wall_start).total_seconds()) if wall_start and wall_end else 0.0
            saved[day.isoformat()] = {
                "status": "CLOSED", "recorded_seconds": totals.recorded_seconds,
                "covered_seconds": totals.covered_seconds, "overlap_seconds": totals.overlap_seconds,
                "wall_clock_seconds": wall_elapsed,
                "potential_untracked_seconds": max(0.0, wall_elapsed - totals.covered_seconds),
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.work_ledger.close_day(day, saved[day.isoformat()])
            atomic_write_text(day_path, json.dumps(saved, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Close workday", redact_sensitive_text(exc), parent=window)
            return
        self._append_action_log("workday_closed", {
            "date": day.isoformat(), "recorded_seconds": totals.recorded_seconds,
            "covered_seconds": totals.covered_seconds, "overlap_seconds": totals.overlap_seconds,
        })
        status_var.set("Day closed and reconciliation saved.")
        messagebox.showinfo("Workday closed", "Reconciliation saved for %s." % day.isoformat(), parent=window)

    def _sync_clickup_task_updates(self, features, callback):
        def worker():
            client = self._create_clickup_client()
            fields = client.request("GET", "/list/%s/field" % client.list_id).get("fields", [])
            matches = [field for field in fields if str(field.get("name", "")).strip().casefold() == "last update date"]
            if len(matches) != 1 or matches[0].get("type") != "date":
                raise ClickUpError("Expected exactly one ClickUp Date custom field named 'Last Update date'.")
            field_id = matches[0]["id"]
            outcomes = []
            for feature in features:
                record = self.feature_map.get(feature)
                if record is None or record.last_update_dt is None or record.frequency not in FREQUENCY_DELTAS:
                    outcomes.append((feature, "FAILED", "Local last_update or frequency is missing."))
                    self.clickup_state.update_feature(feature, "task_sync", {"status": "FAILED", "error": "Local date/frequency missing"})
                    continue
                start_date = record.last_update_dt.date()
                due_date = start_date + FREQUENCY_DELTAS[record.frequency]
                start_ms = int(datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc).timestamp() * 1000)
                due_ms = int(datetime(due_date.year, due_date.month, due_date.day, tzinfo=timezone.utc).timestamp() * 1000)
                try:
                    prior = self.clickup_state.read().get(feature, {}).get("task_sync", {})
                    if (
                        prior.get("status") == "SYNCED"
                        and prior.get("start_date_ms") == start_ms
                        and prior.get("due_date_ms") == due_ms
                        and prior.get("custom_field_value") == start_ms
                        and prior.get("clickup_task_id")
                    ):
                        outcomes.append((feature, "ALREADY SYNCED", ""))
                        continue
                    cached = prior.get("clickup_task_id")
                    task = client.resolve_task(feature, cached)
                    task_id = str(task["id"])
                    self.clickup_state.update_feature(feature, "task_sync", {"clickup_task_id": task_id})
                    client.update_task_fields(task_id, {
                        "start_date": start_ms, "start_date_time": False,
                        "due_date": due_ms, "due_date_time": False,
                    })
                    client.set_custom_field(task_id, field_id, start_ms)
                    client.verify_task_dates(task_id, start_ms, due_ms, field_id, start_ms)
                    self.clickup_state.update_feature(feature, "task_sync", {
                        "status": "SYNCED", "error": None, "last_synced_at": datetime.now(timezone.utc).isoformat(),
                        "start_date_ms": start_ms, "due_date_ms": due_ms, "custom_field_value": start_ms,
                    })
                    outcomes.append((feature, "SYNCED", ""))
                except Exception as exc:
                    error = redact_sensitive_text(exc)
                    self.clickup_state.update_feature(feature, "task_sync", {"status": "FAILED", "error": error})
                    outcomes.append((feature, "FAILED", error))
            return outcomes

        def done(result, error):
            self._reload_clickup_sync_cache()
            if error:
                callback("Task sync failed: " + redact_sensitive_text(error))
                return
            failures = sum(1 for _, status, _ in result if status not in {"SYNCED", "ALREADY SYNCED"})
            for feature, status, detail in result:
                self._append_action_log("clickup_task_sync", {"feature_number": feature, "status": status, "error": detail})
                self._log("feature_%s ClickUp task sync: %s%s" % (feature, status, (" - " + detail) if detail else ""))
            self._refresh_table()
            callback("Task sync complete: %d success, %d need review." % (len(result) - failures, failures))

        self._submit_work_job("Sync ClickUp task updates", worker, done)

    def _sync_clickup_attachments(self, features, callback):
        def worker():
            client = self._create_clickup_client()
            results = []
            for feature in features:
                canonical = self._canonical_outputs_dir() / (feature + ".sqlite")
                try:
                    if not canonical.is_file() or self.sqlite_compare_states.get(feature) != "SAME":
                        raise ClickUpError("Accepted canonical SQLite is not verified as SAME.")
                    status, task_id, detail = self._upload_accepted_attachment(client, feature, canonical)
                    results.append((feature, status, None))
                except Exception as exc:
                    error = redact_sensitive_text(exc)
                    domain = self.clickup_state.read().get(feature, {}).get("attachment_sync", {})
                    status = domain.get("status", "UPLOAD FAILED") if isinstance(domain, dict) else "UPLOAD FAILED"
                    if status not in {"UPLOAD UNKNOWN", "UPLOAD IN FLIGHT"}:
                        status = "UPLOAD FAILED"
                        self.clickup_state.update_feature(feature, "attachment_sync", {"status": status, "error": error})
                    results.append((feature, status, error))
            return results

        def done(result, error):
            self._reload_clickup_sync_cache()
            if error:
                callback("Attachment sync failed: " + redact_sensitive_text(error))
                return
            failures = sum(1 for _, status, _ in result if status not in {"CURRENT", "OLD ATTACHMENT REMAINS", "ALREADY SYNCED"})
            self._refresh_table()
            callback("Attachment sync complete: %d success, %d failed." % (len(result) - failures, failures))

        self._submit_work_job("Sync accepted ClickUp attachments", worker, done)

    def _upload_accepted_attachment(self, client, feature, canonical):
        digest = sha256_file(canonical)
        record = self.feature_map.get(feature)

        if record is None or not record.clickup_task_id:
            raise ClickUpError(
                "Permanent ClickUp task ID is missing for feature_%s."
                % feature
            )

        task_id = str(record.clickup_task_id)

        task = client.validate_task_identity(
            task_id,
            feature,
        )

        if str(task.get("id", "")) != task_id:
            raise ClickUpError(
                "ClickUp returned a different task ID; "
                "automatic reassignment is forbidden."
            )

        self.clickup_state.update_feature(
            feature,
            "task_sync",
            {
                "clickup_task_id": task_id,
            },
        )
        attempt_id = self.clickup_state.claim_attachment_upload(feature, task_id, canonical, digest)
        if attempt_id is None:
            status = self.clickup_state.read().get(feature, {}).get("attachment_sync", {}).get("status", "CURRENT")
            return status, task_id, "Already uploaded and verified"
        request_started = [False]
        try:
            def before_upload():
                request_started[0] = True
                self.clickup_state.update_feature(feature, "attachment_sync", {
                    "status": "UPLOAD IN FLIGHT", "request_started_at": datetime.now(timezone.utc).isoformat(),
                })

            upload = client.upload_and_verify_attachment(task_id, canonical, before_upload=before_upload)
        except Exception as exc:
            status = "UPLOAD UNKNOWN" if request_started[0] else "UPLOAD FAILED"
            self.clickup_state.update_feature(feature, "attachment_sync", {
                "status": status, "error": redact_sensitive_text(exc),
            })
            raise
        attachment = upload["attachment"]
        status = "OLD ATTACHMENT REMAINS" if upload["old_managed_attachment_remains"] else "CURRENT"
        self.clickup_state.update_feature(feature, "attachment_sync", {
            "status": status, "attachment_id": str(attachment.get("id", "")),
            "file_name": canonical.name, "source_path": str(canonical), "sha256": digest,
            "uploaded_at": datetime.now(timezone.utc).isoformat(), "error": None,
            "attempt_id": None,
        })
        return status, task_id, ""

    def _sync_work_intervals(self, interval_ids, day, callback):
        def worker():
            active = self.work_ledger.active_intervals()
            if active:
                raise ClickUpError("Active work timers block end-of-day time sync.")
            client = self._create_clickup_client()
            team_id = os.environ.get("CLICKUP_TEAM_ID", "").strip()
            if not team_id:
                raise ClickUpError("Set CLICKUP_TEAM_ID in the application .env before time sync.")
            results = []
            for interval_id in interval_ids:
                interval = self.work_ledger.get_interval(interval_id)
                if interval.sync_status == "synced":
                    results.append((interval.id, "ALREADY SYNCED", ""))
                    continue
                if interval.ended_at is None:
                    results.append((interval.id, "FAILED", "Active intervals cannot be submitted."))
                    continue
                record = self.feature_map.get(interval.feature_number)

                cached_task = (
                    record.clickup_task_id
                    if record is not None
                    else ""
                )
                marker = "FeatureManager interval %s" % interval.id
                start_ms = to_unix_milliseconds(interval.started_at)
                end_ms = to_unix_milliseconds(interval.ended_at)
                if end_ms <= start_ms:
                    results.append((interval.id, "FAILED", "Interval collapses at ClickUp millisecond precision."))
                    continue
                task_id = None
                token = None
                submission_started = False
                remote_entry_observed = False
                try:
                    if not cached_task:
                        raise ClickUpError(
                            "Permanent ClickUp task ID is missing for feature_%s."
                            % interval.feature_number
                        )

                    task = client.validate_task_identity(
                        cached_task,
                        interval.feature_number,
                    )
                    task_id = str(task["id"])
                    self.clickup_state.update_feature(interval.feature_number, "task_sync", {"clickup_task_id": task_id})
                    token, reconcile_only = self.work_ledger.claim_time_sync(interval.id, task_id)
                    found = client.find_time_entry_by_marker(
                        team_id, start_ms - 86_400_000, end_ms + 86_400_000, task_id, marker,
                    )
                    if found is not None:
                        remote_entry_observed = True
                        client.verify_time_entry(found, task_id, start_ms, end_ms, marker)
                    elif reconcile_only:
                        self.work_ledger.fail_time_sync(
                            interval.id, token, "No matching ClickUp entry is visible; automatic resubmission is blocked.",
                            uncertain=True,
                        )
                        results.append((interval.id, "UNKNOWN", "No matching ClickUp entry is visible; verify ClickUp before release."))
                        continue
                    else:
                        description = "%s | %s%s" % (
                            marker, interval.category,
                            (" | " + interval.notes) if interval.notes else "",
                        )
                        self.work_ledger.mark_time_submission_started(interval.id, token)
                        submission_started = True
                        created = client.create_completed_time_entry(team_id, task_id, start_ms, end_ms, description)
                        data = created.get("data", created)
                        entry_id = data.get("id") if isinstance(data, dict) else None
                        found = client.find_time_entry_by_marker(
                            team_id, start_ms - 86_400_000, end_ms + 86_400_000, task_id, marker,
                        )
                        if found is None:
                            raise ClickUpError("Created time entry was not visible in ClickUp read-back; it remains locked for reconciliation.")
                        remote_entry_observed = True
                        if entry_id is not None and str(found.get("id")) != str(entry_id):
                            raise ClickUpError("Returned and read-back time-entry IDs do not match.")
                        client.verify_time_entry(found, task_id, start_ms, end_ms, marker)
                    entry_id = str(found.get("id", ""))
                    if not entry_id:
                        raise ClickUpError("Verified ClickUp time entry has no ID.")
                    self.work_ledger.finish_time_sync(interval.id, token, task_id, entry_id)
                    results.append((interval.id, "SYNCED", entry_id))
                except Exception as exc:
                    error = redact_sensitive_text(exc)
                    uncertain = (submission_started or remote_entry_observed
                                 or interval.sync_status in {"unknown", "in_flight", "reconciling"})
                    if token:
                        try:
                            self.work_ledger.fail_time_sync(interval.id, token, error, uncertain=uncertain)
                        except Exception:
                            pass
                    self.clickup_state.update_feature(interval.feature_number, "time_sync", {
                        "status": "UNKNOWN" if uncertain else "FAILED", "error": error,
                    })
                    results.append((interval.id, "UNKNOWN" if uncertain else "FAILED", error))
            by_feature = {}
            for interval in self.work_ledger.list_intervals(day=day):
                if interval.ended_at is not None:
                    by_feature.setdefault(interval.feature_number, []).append(interval)
            for feature, feature_intervals in by_feature.items():
                if all(item.sync_status == "synced" for item in feature_intervals):
                    self.clickup_state.update_feature(feature, "time_sync", {
                        "status": "SYNCED", "error": None,
                        "last_synced_at": datetime.now(timezone.utc).isoformat(),
                    })
                elif any(item.sync_status == "failed" for item in feature_intervals):
                    self.clickup_state.update_feature(feature, "time_sync", {"status": "FAILED"})
                elif any(item.sync_status in {"in_flight", "unknown", "reconciling"} for item in feature_intervals):
                    self.clickup_state.update_feature(feature, "time_sync", {"status": "UNKNOWN"})
                else:
                    self.clickup_state.update_feature(feature, "time_sync", {"status": "PENDING"})
            return results

        def done(result, error):
            self._reload_clickup_sync_cache()
            if error:
                callback("Time sync stopped: " + redact_sensitive_text(error))
                return
            successes = sum(1 for _, status, _ in result if status in {"SYNCED", "ALREADY SYNCED"})
            failures = sum(1 for _, status, _ in result if status in {"FAILED", "UNKNOWN"})
            self._append_action_log("clickup_time_sync", {
                "day": day.isoformat(), "count": len(result), "successes": successes, "failures": failures,
            })
            callback("Time sync complete: %d synced/already synced, %d failed or requiring reconciliation." % (successes, failures))

        self._submit_work_job("Sync reviewed ClickUp time entries", worker, done)

    def _open_work_ledger(self):
        if not self.work_ledger_ready:
            messagebox.showwarning("Work Ledger", "Work tracking is not available.")
            return

        window = tk.Toplevel(self)
        window.title("Work Ledger")
        self._maximize_window(window)
        window.transient(self)

        filters = ttk.Frame(window)
        filters.pack(fill=tk.X, padx=10, pady=8)
        ttk.Label(filters, text="Work date:").pack(side=tk.LEFT)
        day_var = tk.StringVar(value=datetime.now().astimezone().date().isoformat())
        ttk.Entry(filters, textvariable=day_var, width=12).pack(side=tk.LEFT, padx=5)
        ttk.Label(filters, text="YYYY-MM-DD").pack(side=tk.LEFT, padx=(0, 8))
        status_var = tk.StringVar(value="")
        ttk.Label(filters, textvariable=status_var).pack(side=tk.RIGHT)

        columns = ("feature", "category", "started", "ended", "duration", "notes")
        listing = ttk.Treeview(window, columns=columns, show="headings", selectmode="browse")
        labels = {
            "feature": "Feature", "category": "Category", "started": "Start (local)",
            "ended": "End (local)", "duration": "Duration", "notes": "Notes",
        }
        widths = {"feature": 90, "category": 110, "started": 175, "ended": 175, "duration": 100, "notes": 420}
        for column in columns:
            listing.heading(column, text=labels[column])
            listing.column(column, width=widths[column], anchor=tk.W if column == "notes" else tk.CENTER)
        listing.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        row_map = {}

        def refresh():
            try:
                selected_day = date.fromisoformat(day_var.get().strip())
            except ValueError:
                status_var.set("Enter a valid date as YYYY-MM-DD.")
                return

            status_var.set("Loading intervals...")

            def load_intervals():
                return (
                    self.work_ledger.list_intervals(day=selected_day),
                    self.work_ledger.daily_totals(selected_day),
                )

            def done(payload, error):
                if not window.winfo_exists():
                    return
                if error:
                    status_var.set(redact_sensitive_text(error))
                    return
                intervals, totals = payload
                for row in listing.get_children():
                    listing.delete(row)
                row_map.clear()
                now = datetime.now(timezone.utc)
                for interval in intervals:
                    end = interval.ended_at
                    duration_seconds = ((end or now) - interval.started_at).total_seconds()
                    listing.insert(
                        "", tk.END, iid=interval.id,
                        values=(interval.feature_number, interval.category,
                                self._format_interval_time(interval.started_at),
                                self._format_interval_time(end) if end else "ACTIVE",
                                self._format_work_duration(duration_seconds), interval.notes),
                    )
                    row_map[interval.id] = interval
                status_var.set(
                    "%d interval(s) | recorded %s | parallel overlap %s" % (
                        len(intervals),
                        self._format_work_duration(totals.recorded_seconds),
                        self._format_work_duration(totals.overlap_seconds),
                    )
                )

            self._submit_work_job(
                "Load work intervals",
                load_intervals,
                done,
            )

        def selected_interval():
            selection = listing.selection()
            return row_map.get(selection[0]) if selection else None

        def add_interval():
            self._show_interval_editor(window, None, refresh)

        def edit_interval():
            interval = selected_interval()
            if interval is None:
                messagebox.showinfo("Edit interval", "Select an interval first.", parent=window)
                return
            self._show_interval_editor(window, interval, refresh)

        def delete_interval():
            interval = selected_interval()
            if interval is None:
                messagebox.showinfo("Delete interval", "Select an interval first.", parent=window)
                return
            if not messagebox.askyesno(
                "Delete interval",
                "Remove this interval from normal ledger views? The audit record will remain.",
                parent=window,
            ):
                return

            def done(_result, error):
                if error:
                    status_var.set(redact_sensitive_text(error))
                    return
                current = self.active_work_intervals.get(interval.feature_number)
                if current is not None and current.id == interval.id:
                    self.active_work_intervals.pop(interval.feature_number, None)
                self._append_action_log(
                    "work_interval_deleted",
                    {"feature_number": interval.feature_number, "interval_id": interval.id},
                )
                self._refresh_active_work_cache(refresh)

            self._submit_work_job(
                "Delete work interval",
                lambda: self.work_ledger.delete_interval(interval.id, expected_revision=interval.revision),
                done,
            )

        def stop_interval():
            interval = selected_interval()
            if interval is None or not interval.is_active:
                messagebox.showinfo("Stop Work", "Select an active interval first.", parent=window)
                return
            if not messagebox.askyesno("Stop Work", "Stop the selected timer now?", parent=window):
                return
            self._stop_work_features(
                [interval.feature_number],
                interval_ids={interval.feature_number: interval.id},
                on_complete=refresh,
            )

        actions = ttk.Frame(window)
        actions.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(filters, text="Refresh", command=refresh).pack(side=tk.LEFT, padx=5)
        ttk.Button(actions, text="Add missed interval", command=add_interval).pack(side=tk.LEFT)
        ttk.Button(actions, text="Edit selected", command=edit_interval).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Stop selected timer", command=stop_interval).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Delete selected", command=delete_interval).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Close", command=window.destroy).pack(side=tk.RIGHT)
        refresh()

    def _show_interval_editor(self, parent, interval, on_saved):
        editing = interval is not None
        window = tk.Toplevel(parent)
        window.title("Edit Work Interval" if editing else "Add Missed Work Interval")
        window.transient(parent)
        window.grab_set()
        window.resizable(False, False)

        feature_var = tk.StringVar(value=interval.feature_number if editing else "")
        category_var = tk.StringVar(value=interval.category if editing else ("work" if "work" in self.work_categories else "development"))
        start_var = tk.StringVar(value=self._format_interval_time(interval.started_at) if editing else datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"))
        end_var = tk.StringVar(value=self._format_interval_time(interval.ended_at) if editing and interval.ended_at else "")
        notes_var = tk.StringVar(value=interval.notes if editing else "")
        status_var = tk.StringVar(value="")

        ttk.Label(window, text="Feature number:").grid(row=0, column=0, padx=10, pady=6, sticky=tk.W)
        feature_entry = ttk.Entry(window, textvariable=feature_var, width=28)
        feature_entry.grid(row=0, column=1, padx=10, pady=6)
        ttk.Label(window, text="Category:").grid(row=1, column=0, padx=10, pady=6, sticky=tk.W)
        category_entry = ttk.Combobox(window, textvariable=category_var, values=self.work_categories, state="readonly", width=25)
        category_entry.grid(row=1, column=1, padx=10, pady=6)
        ttk.Label(window, text="Start (local):").grid(row=2, column=0, padx=10, pady=6, sticky=tk.W)
        ttk.Entry(window, textvariable=start_var, width=28).grid(row=2, column=1, padx=10, pady=6)
        ttk.Label(window, text="End (local):").grid(row=3, column=0, padx=10, pady=6, sticky=tk.W)
        ttk.Entry(window, textvariable=end_var, width=28).grid(row=3, column=1, padx=10, pady=6)
        ttk.Label(window, text="Notes:").grid(row=4, column=0, padx=10, pady=6, sticky=tk.W)
        ttk.Entry(window, textvariable=notes_var, width=52).grid(row=4, column=1, padx=10, pady=6)
        ttk.Label(window, text="Use YYYY-MM-DD HH:MM:SS. Leave End blank only for an active timer.").grid(
            row=5, column=0, columnspan=2, padx=10, pady=6, sticky=tk.W,
        )
        ttk.Label(window, textvariable=status_var, wraplength=390).grid(row=6, column=0, columnspan=2, padx=10, pady=4, sticky=tk.W)

        buttons = ttk.Frame(window)
        buttons.grid(row=7, column=0, columnspan=2, padx=10, pady=10, sticky=tk.E)
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side=tk.RIGHT, padx=(6, 0))

        def save():
            try:
                feature = feature_var.get().strip()
                category = category_var.get().strip()
                notes = notes_var.get()
                started_at = self._parse_interval_time(start_var.get(), "Start time", required=True)
                ended_at = self._parse_interval_time(end_var.get(), "End time", required=not editing)
                if not editing and ended_at is None:
                    raise WorkLedgerValidationError("An added missed interval needs an end time.")
                if ended_at is not None and ended_at <= started_at:
                    raise WorkLedgerValidationError("End time must be later than start time.")
            except Exception as exc:
                status_var.set(redact_sensitive_text(exc))
                return

            save_button.configure(state="disabled")
            status_var.set("Saving interval...")

            def worker():
                if editing:
                    return self.work_ledger.update_interval(
                        interval.id,
                        feature_number=feature,
                        category=category,
                        notes=notes,
                        started_at=started_at,
                        ended_at=ended_at,
                        expected_revision=interval.revision,
                    )
                return self.work_ledger.add_interval(
                    feature,
                    category=category,
                    notes=notes,
                    started_at=started_at,
                    ended_at=ended_at,
                )

            def done(saved, error):
                if not window.winfo_exists():
                    return
                if error:
                    status_var.set(redact_sensitive_text(error))
                    save_button.configure(state="normal")
                    return
                self._append_action_log(
                    "work_interval_edited" if editing else "work_interval_added",
                    {"feature_number": saved.feature_number, "interval_id": saved.id,
                     "category": saved.category, "started_at": saved.started_at.isoformat(),
                     "ended_at": saved.ended_at.isoformat() if saved.ended_at else None},
                )
                try:
                    window.grab_release()
                except Exception:
                    pass
                window.destroy()
                self._refresh_active_work_cache(on_saved)

            self._submit_work_job("Save work interval", worker, done)

        save_button = ttk.Button(buttons, text="Save", command=save)
        save_button.pack(side=tk.RIGHT)
        feature_entry.focus_set()

    def _start_pack_selected(self):
        if not self._require_daily_clickup_sync("Pack selected"):
            return
        features = self._get_selected_features()
        if not features:
            messagebox.showinfo("Pack", "No features selected.")
            return

        script_runs_dir = self._get_script_runs_dir()
        if not script_runs_dir.exists():
            messagebox.showerror("Pack", f"Script Runs directory does not exist:\n{script_runs_dir}")
            return
        if not self._try_begin_busy("Pack"):
            return

        session_dir = self._start_session_dir("Pack", {"selected_count": len(features)})
        self._log(f"Packing {len(features)} features...")

        def worker():
            def progress(res: PackResult):
                self._ui_post(self._log, f"feature_{res.feature}: {res.status.upper()} - {res.message}")

            pack_features(features=features, script_runs_dir=script_runs_dir, progress_callback=progress)
            self._ui_post(self._on_pack_finished)
            self._ui_post(self._log, f"Packing finished. Session dir: {session_dir}")
            self._append_action_log(
                "pack_finished",
                {"selected_count": len(features), "session_dir": str(session_dir)},
            )

        threading.Thread(target=worker, daemon=True).start()

    def _start_batch_selected(self):
        if not self._require_daily_clickup_sync("Batch outputs"):
            return
        features = self._get_selected_features()
        if not features:
            messagebox.showinfo("Batch", "No features selected.")
            return

        script_runs_dir = self._get_script_runs_dir()
        if not script_runs_dir.exists():
            messagebox.showerror("Batch", f"Script Runs directory does not exist:\n{script_runs_dir}")
            return
        if not self._try_begin_busy("Batch"):
            return

        session_dir = self._start_session_dir("Batch", {"selected_count": len(features)})
        self._log(f"Batching outputs for {len(features)} features...")
        self._append_action_log(
            "batch_started",
            {"selected_count": len(features), "session_dir": str(session_dir)},
        )
        def worker():
            def progress(res: BatchResult):
                self._ui_post(self._on_batch_result, res, session_dir)

            batch_outputs(features=features, script_runs_dir=script_runs_dir, progress_callback=progress)
            self._ui_post(self._log, f"Batching finished. Session dir: {session_dir}")
            self._append_action_log(
                "batch_finished",
                {"selected_count": len(features), "session_dir": str(session_dir)},
            )
            self._ui_post(self._clear_busy, "Batch")

        threading.Thread(target=worker, daemon=True).start()

    def _on_batch_result(self, result: BatchResult, session_dir: Path):
        if result.person_count is not None or result.output_class:
            self.batch_metadata[result.feature] = {
                "feature_number": result.feature,
                "project_type": result.category,
                "person_count": result.person_count,
                "output_class": result.output_class or "REVIEW",
                "output_folder": result.output_folder,
                "output_path": result.dest_path,
            }
        self._log(
            "feature_%s: %s [Project Type: %s | Persons: %s | Output Class: %s] - %s" % (
                result.feature,
                result.status.upper(),
                result.category,
                result.person_count if result.person_count is not None else "REVIEW",
                result.output_class or "REVIEW",
                result.message,
            )
        )
        self._append_action_log("batch_feature_result", {
            "feature_number": result.feature,
            "status": result.status,
            "project_type": result.category,
            "person_count": result.person_count,
            "output_class": result.output_class,
            "output_folder": result.output_folder,
            "output_path": result.dest_path,
            "metadata_error": result.metadata_error,
            "message": result.message,
            "metadata_error": result.metadata_error,
            "session_dir": str(session_dir),
        })
        self._refresh_table()

    def _on_pack_finished(self):
        self._clear_busy("Pack")
        self._log("Packing finished.")
        self.pack_finished_once = True
        if self.btn_copy_zips is not None:
            self.btn_copy_zips.configure(state="normal")
        self._log("Copy packed zips is now enabled.")

    def _start_copy_packed_zips(self):
        if not self.pack_finished_once:
            messagebox.showinfo(
                "Copy packed zips",
                "Please run 'Pack selected' first and wait for it to finish."
            )
            return

        features = self._get_selected_features()
        if not features:
            messagebox.showinfo("Copy packed zips", "No features selected.")
            return

        script_runs_dir = self._get_script_runs_dir()
        if not script_runs_dir.exists():
            messagebox.showerror(
                "Copy packed zips",
                f"Script Runs directory does not exist:\n{script_runs_dir}"
            )
            return
        if not self._try_begin_busy("Copy packed zips"):
            return

        dest_dir = APP_ROOT / "Packed_Zips"
        session_dir = self._start_session_dir(
            "Copy packed zips",
            {"selected_count": len(features), "dest_dir": str(dest_dir)},
        )

        self._append_action_log(
            "copy_zips_started",
            {"selected_count": len(features), "dest_dir": str(dest_dir), "session_dir": str(session_dir)},
        )
        def worker():
            def progress(res: CopyZipResult):
                self._ui_post(self._log, f"feature_{res.feature}: {res.status.upper()} - {res.message}")

            copy_packed_zips(
                features=features,
                script_runs_dir=script_runs_dir,
                dest_dir=dest_dir,
                progress_callback=progress,
            )

            self._ui_post(self._log, f"Copy packed zips finished. Session dir: {session_dir}")
            self._append_action_log(
                "copy_zips_finished",
                {"selected_count": len(features), "dest_dir": str(dest_dir), "session_dir": str(session_dir)},
            )
            self._ui_post(self._clear_busy, "Copy packed zips")


        threading.Thread(target=worker, daemon=True).start()

    def _start_validate_selected(self):
        if not self._require_daily_clickup_sync("Validate selected"):
            return
        features = self._get_selected_features()
        if not features:
            messagebox.showinfo("Validate", "No features selected.")
            return

        script_runs_dir = self._get_script_runs_dir()
        if not script_runs_dir.exists():
            messagebox.showerror("Validate", f"Script Runs directory does not exist:\n{script_runs_dir}")
            return

        if not self._try_begin_busy("Validate"):
            return

        session_dir = self._start_session_dir("Validate", {"selected_count": len(features)})
        self._append_action_log(
            "validate_started",
            {"selected_count": len(features), "session_dir": str(session_dir)},
        )

        self._log(f"Validating {len(features)} features...")

        def worker():
            ok = 0
            warn = 0

            for f in features:
                folder = script_runs_dir / f"feature_{f}"
                issues = []

                if not folder.exists():
                    issues.append("missing folder")
                else:
                    if not (folder / "main.py").exists():
                        issues.append("missing main.py")
                    if not (folder / "script.config").exists():
                        issues.append("missing script.config")

                    # SQLite output sanity: any *.sqlite present
                    try:
                        has_sqlite = any(folder.glob("*.sqlite"))
                    except Exception:
                        has_sqlite = False
                    if not has_sqlite:
                        issues.append("no .sqlite output")

                if issues:
                    warn += 1
                    self._ui_post(self._log, f"feature_{f}: VALIDATE WARN - " + "; ".join(issues))
                else:
                    ok += 1
                    self._ui_post(self._log, f"feature_{f}: VALIDATE OK")

            self._ui_post(self._log, f"Validation complete: ok={ok}, warn={warn}. Session dir: {session_dir}")
            self._append_action_log(
                "validate_finished",
                {"selected_count": len(features), "ok": ok, "warn": warn, "session_dir": str(session_dir)},
            )
            self._ui_post(self._clear_busy, "Validate")

        threading.Thread(target=worker, daemon=True).start()

    def _start_clean_directories_selected(self):
        if not self._require_daily_clickup_sync("Clean directories"):
            return
        features = self._get_selected_features()
        if not features:
            messagebox.showinfo("Clean directories", "No features selected.")
            return

        script_runs_dir = self._get_script_runs_dir()
        if not script_runs_dir.exists():
            messagebox.showerror("Clean directories", f"Script Runs directory does not exist:\n{script_runs_dir}")
            return

        if not self._try_begin_busy("Clean directories"):
            return

        confirm = messagebox.askyesno(
            "Clean directories",
            "This will delete all extra files and folders inside the selected feature directories.\n\n"
            "Kept per feature:\n"
            "- main.py\n"
            "- script.config\n"
            "- preferred sqlite (XXXX.sqlite), otherwise the most recent *.sqlite\n\n"
            "Continue?"
        )
        if not confirm:
            self._clear_busy("Clean directories")
            return

        session_dir = self._start_session_dir("Clean directories", {"selected_count": len(features)})
        self._log(f"Cleaning directories for {len(features)} features...")
        self._append_action_log(
            "clean_directories_started",
            {"selected_count": len(features), "session_dir": str(session_dir)},
        )

        def worker():
            from ..workers.clean_feature_directories_worker import clean_feature_directories, CleanDirectoryResult

            def progress(res: CleanDirectoryResult):
                kept_part = f" | kept: {', '.join(res.kept)}" if res.kept else ""
                removed_part = f" | removed: {', '.join(res.removed)}" if res.removed else ""
                self._ui_post(
                    self._log,
                    f"feature_{res.feature}: {res.status.upper()} - {res.message}{kept_part}{removed_part}"
                )

            try:
                clean_feature_directories(
                    features=features,
                    script_runs_dir=script_runs_dir,
                    progress_callback=progress,
                )
                self._ui_post(self._log, f"Clean directories finished. Session dir: {session_dir}")
            finally:
                self._append_action_log(
                    "clean_directories_finished",
                    {"selected_count": len(features), "session_dir": str(session_dir)},
                )
                self._ui_post(self._clear_busy, "Clean directories")

        threading.Thread(target=worker, daemon=True).start()
    def _open_python_settings(self):
        """Set the external Python used for ordinary user features."""
        window = tk.Toplevel(self)
        window.title("Python Interpreter Settings")
        window.transient(self)
        window.resizable(False, False)
        window.grab_set()

        frame = ttk.Frame(window, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="Python for your scripts (optional):",
        ).pack(anchor=tk.W)

        selected = tk.StringVar(
            value=str(self.runtime_settings.script_python or "")
        )

        entry_row = ttk.Frame(frame)
        entry_row.pack(fill=tk.X, pady=8)

        entry = ttk.Entry(
            entry_row,
            textvariable=selected,
            width=65,
        )
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def browse():
            chosen = filedialog.askopenfilename(
                parent=window,
                title="Choose Python executable",
                filetypes=[("All files", "*.*")],
            )
            if chosen:
                selected.set(chosen)

        ttk.Button(
            entry_row,
            text="Browse...",
            command=browse,
        ).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            frame,
            text=(
                "Bundled demonstrations work without an external "
                "Python installation. Your own scripts use the "
                "Python selected here."
            ),
            wraplength=480,
        ).pack(anchor=tk.W, pady=6)

        def save_selection(clear=False):
            raw = "" if clear else selected.get().strip()

            try:
                candidate = (
                    Path(raw).expanduser().resolve()
                    if raw else None
                )

                version = (
                    probe_script_python(candidate)
                    if candidate else None
                )

                updated = replace(
                    self.runtime_settings,
                    script_python=candidate,
                )

                destination = save_runtime_settings(updated)

            except (RuntimeSettingsError, OSError, ValueError) as exc:
                messagebox.showerror(
                    "Python interpreter",
                    str(exc),
                    parent=window,
                )
                return

            self.runtime_settings = updated

            if candidate:
                self._log(
                    f"Selected script Python: {candidate} "
                    f"(Python {version})"
                )
            else:
                self._log(
                    "External script Python selection cleared."
                )

            self._log(
                f"Runtime settings saved: {destination}"
            )

            window.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(
            buttons,
            text="Clear selection",
            command=lambda: save_selection(True),
        ).pack(side=tk.LEFT)

        ttk.Button(
            buttons,
            text="Cancel",
            command=window.destroy,
        ).pack(side=tk.RIGHT, padx=5)

        ttk.Button(
            buttons,
            text="Validate and Save",
            command=save_selection,
        ).pack(side=tk.RIGHT)

        entry.focus_set()
        window.protocol("WM_DELETE_WINDOW", window.destroy)

    def _run_bundled_demo(self):
        """Run packaged SQLite demo without changing user records."""
        if not self._try_begin_busy("Demo"):
            return

        self._log("Starting offline SQLite demonstration...")
        self._append_action_log("demo_started", {})

        def worker():
            try:
                outcome = run_offline_demo(self.runtime_settings)
                self._ui_post(
                    self._finish_bundled_demo,
                    outcome,
                    None,
                )
            except Exception as exc:
                self._ui_post(
                    self._finish_bundled_demo,
                    None,
                    str(exc),
                )

        threading.Thread(
            target=worker,
            daemon=True,
        ).start()

    def _finish_bundled_demo(self, outcome, error):
        self._clear_busy("Demo")

        if error:
            self._log(
                f"Bundled demo failed: "
                f"{redact_sensitive_text(error)}"
            )

            messagebox.showerror(
                "Automator demonstration",
                error,
            )

            self._append_action_log(
                "demo_finished",
                {"ok": False},
            )
            return

        self._log(
            f"Demonstration: {outcome.message}"
        )

        self._log(
            f"Demo output: {outcome.output_database}"
        )

        self._append_action_log(
            "demo_finished",
            {
                "ok": outcome.succeeded,
                "run_directory": str(outcome.run_directory),
            },
        )

        if outcome.succeeded:
            messagebox.showinfo(
                "Automator demonstration",
                "Success! The packaged feature ran and produced "
                "a SQLite database.\n\n"
                f"Output: {outcome.output_database}",
            )
        else:
            messagebox.showerror(
                "Automator demonstration",
                f"Demo failed: {outcome.message}",
            )

    def _on_close(self):
        self._save_gui_state()
        if self._work_jobs_pending:
            self._close_requested = True
            try:
                self.work_menu_button.configure(state="disabled")
            except Exception:
                pass
            self.work_status_var.set("Work tracking: finishing a local ledger update")
            return
        self._finish_close()

    def _finish_close(self):
        self._close_requested = True
        self._rotate_run_history_if_needed()
        self._save_gui_state()
        self.destroy()


def main():
    try:
        initialize_first_run(RUNTIME_SETTINGS)
    except FirstRunError as exc:
        alert = tk.Tk()
        alert.withdraw()
        try:
            messagebox.showerror(
                "Automator startup",
                str(exc),
                parent=alert,
            )
        finally:
            alert.destroy()
        return

    FeatureManagerGUI().mainloop()


if __name__ == "__main__":
    main()
