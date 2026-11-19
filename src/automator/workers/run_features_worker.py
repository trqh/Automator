#!/usr/bin/env python3
from __future__ import annotations
import re
import os
import csv
import sys
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Callable, Dict, Iterable, List, Optional, Tuple
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from ..infrastructure.browser_utils import resolve_chromedriver_path

import psutil  # pip install psutil
from ..infrastructure.storage_paths import legacy_storage
from ..infrastructure.runtime_config import (
    RuntimeSettings, resolve_feature_interpreter, worker_runtime_settings, worker_workspace,
)

# ===================== CONFIGURATION =====================

RUNTIME_SETTINGS = worker_runtime_settings(__file__)
APP_ROOT = RUNTIME_SETTINGS.data_root
DEFAULT_SCRIPT_RUNS_DIR = worker_workspace(RUNTIME_SETTINGS)
DEFAULT_TIMEOUT = 75000  # seconds
STDERR_MAX_CHARS = 4000
STORAGE = legacy_storage(APP_ROOT)
# =========================================================
# ===================== HARD CANCEL REGISTRY =====================

_ACTIVE_PROCS_LOCK = threading.Lock()
_ACTIVE_PROCS: dict[str, subprocess.Popen] = {}
# ===================== RUN HISTORY =====================

_RUN_HISTORY_LOCK = threading.Lock()

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

def _append_run_history(history_path: Optional[Path], record: dict) -> None:
    if history_path is None:
        return
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _RUN_HISTORY_LOCK:
            with history_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass  # history must never break a run

def _append_jsonl(path: Path, row: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _emit_structured_event(
    logs_dir: Path,
    event: str,
    *,
    correlation_id: Optional[str] = None,
    session_id: Optional[str] = None,
    **data,
) -> None:
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "correlation_id": correlation_id or "",
        "session_id": session_id or "",
        **data,
    }

    _append_jsonl(STORAGE.path("structured_events"), row)
    _append_jsonl(Path(logs_dir) / "events.jsonl", row)

def _register_active_proc(feature: str, proc: subprocess.Popen) -> None:
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS[feature] = proc


def _unregister_active_proc(feature: str) -> None:
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS.pop(feature, None)


def cancel_active_processes() -> int:
    """
    Kill all currently running feature subprocesses and their children.
    Returns number of root processes targeted.
    """
    with _ACTIVE_PROCS_LOCK:
        items = list(_ACTIVE_PROCS.items())

    killed = 0
    for feature, proc in items:
        try:
            if proc.poll() is not None:
                _unregister_active_proc(feature)
                continue

            killed += 1
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)

            for c in children:
                try:
                    c.kill()
                except Exception:
                    pass

            try:
                parent.kill()
            except Exception:
                pass

        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    return killed

@dataclass
class RunResult:
    feature: str
    status: str
    message: str

    started_at: Optional[datetime]
    duration_sec: Optional[float]
    duration_hms: str

    sqlite_copied: bool
    timestamp: Optional[datetime]

    stderr: Optional[str]
    workflow_status: Optional[str] = None
    error_file_path: Optional[str] = None
    error_line_no: Optional[int] = None
    error_source_line: Optional[str] = None
    session_id: Optional[str] = None
    correlation_id: Optional[str] = None
    session_dir: Optional[str] = None
    frequency: Optional[str] = None
    bugs: Optional[str] = None
    notes: Optional[str] = None
    timeout_sec_used: Optional[int] = None

def _seconds_to_hms(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _current_log_basename(logs_dir: Path) -> str:
    # ex: run_log_08_dec_2025
    now = datetime.now()
    return now.strftime("run_log_%d_%b_%Y").lower()


def _log_paths(logs_dir: Path) -> Tuple[Path, Path]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    base = _current_log_basename(logs_dir)
    return logs_dir / f"{base}.log", logs_dir / f"{base}.csv"


def _write_log_line(lock: threading.Lock, logs_dir: Path, result: RunResult) -> None:
    log_path, _ = _log_paths(logs_dir)
    ts = result.timestamp or datetime.now()
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")

    corr_part = f" [corr={result.correlation_id}]" if getattr(result, "correlation_id", None) else ""
    sess_part = f" [session={result.session_id}]" if getattr(result, "session_id", None) else ""

    line = (
        f"[{ts_str}]{corr_part}{sess_part} feature_{result.feature}: {result.status.upper()} "
        f"(duration={result.duration_hms or 'N/A'}, sqlite_copied={result.sqlite_copied}, timeout={('' if result.status != 'timeout' else (result.message or ''))}) - {result.message}\n"
    )

    with lock:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)

def _write_csv_row(lock: threading.Lock, logs_dir: Path, result: RunResult) -> None:
    _, csv_path = _log_paths(logs_dir)

    start_str = result.started_at.strftime("%Y-%m-%d %H:%M:%S") if result.started_at else ""
    status_str = (result.status or "").lower()

    duration_hms = result.duration_hms if result.status == "success" else ""

    error = ""
    if result.status in {"failure", "timeout"}:
        error = redact_sensitive_text(result.message or "")

    timeout_sec = (
        str(result.timeout_sec_used)
        if result.timeout_sec_used is not None
        else ""
    )

    sqlite_copied = "Yes" if result.sqlite_copied else "No"

    workflow_status = (result.workflow_status or "").strip()
    frequency = (result.frequency or "").strip()
    bugs = (result.bugs or "").strip()
    notes = redact_sensitive_text(result.notes or "")

    error_file = redact_sensitive_text(result.error_file_path or "")
    error_line = (
        str(result.error_line_no)
        if result.error_line_no is not None
        else ""
    )

    header = [
        "Time of run start",
        "Feature number",
        "Workflow Status",
        "Frequency",
        "Bug",
        "Notes",
        "Run Result",
        "Duration (HMS)",
        "Timeout Used (sec)",
        "Error",
        "Error File",
        "Error Line",
        "SQLite copied",
        "Session ID",
        "Correlation ID",
    ]

    row = [
        start_str,
        result.feature,
        workflow_status,
        frequency,
        bugs,
        notes,
        status_str,
        duration_hms,
        timeout_sec,
        error,
        error_file,
        error_line,
        sqlite_copied,
        result.session_id or "",
        result.correlation_id or "",
    ]

    with lock:
        file_exists = csv_path.exists()
        with csv_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(header)
            writer.writerow(row)

        # ALSO write to root logs directory (non-session global log)
        try:
            root_logs_dir = STORAGE.path("feature_runs")
            root_logs_dir.mkdir(parents=True, exist_ok=True)

            root_csv_path = root_logs_dir / csv_path.name
            write_header_root = not root_csv_path.exists()

            with root_csv_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if write_header_root:
                    writer.writerow(header)
                writer.writerow(row)


        except Exception as exc:

            print(

                "WARNING: Could not update consolidated run CSV: "

                f"{redact_sensitive_text(exc)}",

                file=sys.stderr,

                flush=True,

            )

def _find_latest_sqlite(feature_dir: Path, feature: str) -> Optional[Path]:
    candidates = list(feature_dir.glob("*.sqlite"))
    if not candidates:
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    alias_name = f"{feature}.sqlite"
    for p in candidates:
        if p.name != alias_name:
            return p
    return candidates[0]


def _kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        parent = psutil.Process(proc.pid)
    except psutil.NoSuchProcess:
        return

    children = parent.children(recursive=True)
    for child in children:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass

    psutil.wait_procs(children, timeout=5)

    try:
        parent.kill()
    except psutil.NoSuchProcess:
        return

    try:
        parent.wait(5)
    except psutil.TimeoutExpired:
        pass

def _extract_failure_location(
    stderr_text: str,
    feature: Optional[str] = None,
) -> Tuple[Optional[str], Optional[int], Optional[str], str]:
    """
    Pick the most useful traceback frame for debugging feature scripts.

    Preference order:
    1. feature_<id>/main.py
    2. any file inside feature_<id>/
    3. first frame outside site-packages / .venv
    4. fallback to final traceback frame
    """
    lines = [ln.rstrip("\r\n") for ln in (stderr_text or "").splitlines()]
    if not lines:
        return None, None, None, ""

    def _parts_for_match(path_str: str) -> tuple[str, ...]:
        """
        Normalize a traceback path into lowercase path parts without assuming
        Windows or POSIX separators.
        """
        raw = (path_str or "").strip().replace("\\", "/")
        parts = [p for p in raw.split("/") if p not in {"", "."}]
        return tuple(p.lower() for p in parts)

    def _endswith_parts(parts: tuple[str, ...], suffix: tuple[str, ...]) -> bool:
        if len(parts) < len(suffix):
            return False
        return parts[-len(suffix):] == suffix

    frames = []
    for idx, line in enumerate(lines):
        m = re.search(r'File "(.+?)", line (\d+)(?:, in .*)?$', line.strip())
        if not m:
            continue

        file_path = m.group(1)
        try:
            line_no = int(m.group(2))
        except Exception:
            line_no = None

        source_line = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
        parts = _parts_for_match(file_path)

        frames.append(
            {
                "file_path": file_path,
                "line_no": line_no,
                "source_line": source_line or None,
                "parts": parts,
            }
        )

    final_error_line = lines[-1].strip() if lines else ""
    if not frames:
        return None, None, None, final_error_line

    feature_dir = f"feature_{feature}".lower() if feature else None

    if feature_dir:
        wanted = (feature_dir, "main.py")
        for fr in frames:
            if _endswith_parts(fr["parts"], wanted):
                return fr["file_path"], fr["line_no"], fr["source_line"], final_error_line

        for fr in frames:
            if feature_dir in fr["parts"]:
                return fr["file_path"], fr["line_no"], fr["source_line"], final_error_line

    for fr in frames:
        parts = fr["parts"]
        if ".venv" not in parts and "site-packages" not in parts:
            return fr["file_path"], fr["line_no"], fr["source_line"], final_error_line

    fr = frames[-1]
    return fr["file_path"], fr["line_no"], fr["source_line"], final_error_line

# ===================== CHROMEDRIVER ENV HANDLING =====================

def _build_subprocess_env(
    *,
    correlation_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Tuple[dict, Optional[str]]:
    env = os.environ.copy()
    if correlation_id:
        env["FEATURE_MANAGER_CORRELATION_ID"] = correlation_id
    if session_id:
        env["FEATURE_MANAGER_SESSION_ID"] = session_id

    chrome_path = resolve_chromedriver_path()
    if chrome_path and os.path.exists(chrome_path):
        env["CHROMEDRIVER_PATH"] = chrome_path
        return env, chrome_path
    return env, None


# ===================== FEATURE RUNNER =====================

def _run_single_feature(
    feature: str,
    script_runs_dir: Path,
    timeout: int,
    base_env: dict,
    history_path: Optional[Path] = None,
    session_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    python_executable: Optional[Path] = None,
) -> RunResult:
    feature = str(feature).strip()
    started_at = datetime.now()

    try:
        timeout = int(timeout)
    except Exception:
        timeout = DEFAULT_TIMEOUT
    if timeout <= 0:
        timeout = DEFAULT_TIMEOUT

    folder_name = f"feature_{feature}"
    feature_dir = script_runs_dir / folder_name
    script_path = feature_dir / "main.py"

    if not feature_dir.exists():
        return RunResult(
            feature=feature,
            status="skipped",
            message="feature directory not found",
            started_at=started_at,
            duration_sec=None,
            duration_hms="",
            sqlite_copied=False,
            timestamp=None,
            stderr=None,
            session_id=session_id,
            correlation_id=correlation_id,
        )

    if not script_path.exists():
        return RunResult(
            feature=feature,
            status="skipped",
            message="main.py not found",
            started_at=started_at,
            duration_sec=None,
            duration_hms="",
            sqlite_copied=False,
            timestamp=None,
            stderr=None,
            session_id=session_id,
            correlation_id=correlation_id,
        )

    start = perf_counter()
    proc: Optional[subprocess.Popen] = None
    # Process-group isolation improves hard-cancel reliability (esp. on Windows)
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(
            [str(python_executable if python_executable is not None else sys.executable), str(script_path)],
            cwd=str(feature_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=base_env,
            **popen_kwargs,
        )

        _register_active_proc(feature, proc)

        try:
            try:
                 _, stderr_text = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                if proc is not None:
                    _kill_process_tree(proc)

                end = perf_counter()
                duration = end - start

                return RunResult(
                    feature=feature,
                    status="timeout",
                    message=f"TIMEOUT after {timeout} seconds",
                    started_at=started_at,
                    duration_sec=duration,
                    duration_hms=_seconds_to_hms(duration),
                    sqlite_copied=False,
                    timestamp=None,
                    stderr=None,
                    session_id=session_id,
                    correlation_id=correlation_id,
                )
        finally:
         _unregister_active_proc(feature)

    except Exception as e:
        if proc is not None:
            _kill_process_tree(proc)

        end = perf_counter()
        duration = end - start

        return RunResult(
            feature=feature,
            status="failure",
            message=f"Exception while starting or running: {e}",
            started_at=started_at,
            duration_sec=duration,
            duration_hms=_seconds_to_hms(duration),
            sqlite_copied=False,
            timestamp=None,
            stderr=None,
            session_id=session_id,
            correlation_id=correlation_id,
        )

    end = perf_counter()
    duration = end - start
    duration_hms = _seconds_to_hms(duration)

    stderr_text = redact_sensitive_text(stderr_text or "")
    if len(stderr_text) > STDERR_MAX_CHARS:
        stderr_text = stderr_text[-STDERR_MAX_CHARS:]

    if proc.returncode != 0:
        error_file_path, error_line_no, error_source_line, final_error_line = _extract_failure_location(
            stderr_text,
            feature=feature,
        )

        msg = f"Exit {proc.returncode}"
        if error_file_path and error_line_no is not None:
            msg += f" at {error_file_path}:{error_line_no}"
        if final_error_line:
            msg += f" - {final_error_line}"

        return RunResult(
            feature=feature,
            status="failure",
            message=msg,
            started_at=started_at,
            duration_sec=duration,
            duration_hms=duration_hms,
            sqlite_copied=False,
            timestamp=None,
            stderr=stderr_text or None,
            workflow_status=None,
            error_file_path=error_file_path,
            error_line_no=error_line_no,
            error_source_line=error_source_line,
            session_id=session_id,
            correlation_id=correlation_id,
        )

    # Success: copy sqlite to feature_number.sqlite (overwrite)
    src_sqlite = _find_latest_sqlite(feature_dir, feature)
    sqlite_copied = False

    if src_sqlite is not None:
        dest_sqlite = feature_dir / f"{feature}.sqlite"
        try:
            shutil.copy2(src_sqlite, dest_sqlite)
            sqlite_copied = True
            msg = f"Completed successfully; sqlite copied from {src_sqlite.name} to {dest_sqlite.name}"
        except Exception as e:
            msg = f"Completed successfully but failed to copy sqlite: {e}"
            return RunResult(
                feature=feature,
                status="success",
                message=msg,
                started_at=started_at,
                duration_sec=duration,
                duration_hms=duration_hms,
                sqlite_copied=False,
                timestamp=datetime.now(),
                stderr=stderr_text or None,
                session_id=session_id,
                correlation_id=correlation_id,
            )
    else:
        msg = "Completed successfully, but no sqlite file found"
        sqlite_copied = False

    return RunResult(
        feature=feature,
        status="success",
        message=msg,
        started_at=started_at,
        duration_sec=duration,
        duration_hms=duration_hms,
        sqlite_copied=sqlite_copied,
        timestamp=datetime.now(),
        stderr=stderr_text or None,
        session_id=session_id,
        correlation_id=correlation_id,
    )


def run_features(
    features: Iterable[str],
    max_workers: int = 4,
    script_runs_dir: Path = DEFAULT_SCRIPT_RUNS_DIR,
    logs_dir: Optional[Path] = None,
    timeout: int = DEFAULT_TIMEOUT,
    progress_callback: Optional[Callable[[RunResult], None]] = None,
    history_path: Optional[Path] = None,
    session_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    workflow_status_map: Optional[Dict[str, str]] = None,
    feature_metadata_map: Optional[Dict[str, dict]] = None,
    feature_timeout_map: Optional[Dict[str, Optional[int]]] = None,
    cancel_event: Optional[threading.Event] = None,
    runtime_settings: Optional[RuntimeSettings] = None,
    demo_features: Optional[Iterable[str]] = None,
    bundled_python: Optional[Path] = None,
    frozen: Optional[bool] = None,
) -> List[RunResult]:

    script_runs_dir = Path(script_runs_dir)
    logs_dir = STORAGE.path("feature_runs") if logs_dir is None else Path(logs_dir)

    features = [str(f).strip() for f in features if str(f).strip()]
    if workflow_status_map is not None:
        workflow_status_map = {str(k).strip(): str(v) for k, v in workflow_status_map.items()}

    if feature_metadata_map is not None:
        feature_metadata_map = {
            str(k).strip(): v if isinstance(v, dict) else {}
            for k, v in feature_metadata_map.items()
        }

    if feature_timeout_map is not None:
        norm = {}
        for k, v in feature_timeout_map.items():
            kk = str(k).strip()
            if not kk:
                continue
            if v is None:
                norm[kk] = None
            else:
                try:
                    iv = int(v)
                    norm[kk] = iv if iv > 0 else None
                except Exception:
                    norm[kk] = None
        feature_timeout_map = norm
    results: List[RunResult] = []
    if not features:
        return results

    # Keep old GUI behavior until I14 supplies runtime_settings. Never execute
    # user scripts with a frozen Automator GUI executable.
    effective_settings = runtime_settings or RUNTIME_SETTINGS
    demo_ids = {str(item).strip() for item in (demo_features or ())}
    resolved_python = {}
    for feature_id in features:
        try:
            resolved_python[feature_id] = resolve_feature_interpreter(
                effective_settings,
                is_demo=feature_id in demo_ids,
                bundled_python=bundled_python,
                frozen=frozen,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Cannot run feature {feature_id}: {exc}"
            ) from exc

    base_env, chrome_path_used = _build_subprocess_env(correlation_id=correlation_id, session_id=session_id)
    # Make shared libraries in the configured feature workspace importable.
    # Preserve any existing PYTHONPATH entries.
    existing_pythonpath = base_env.get("PYTHONPATH", "")
    workspace_pythonpath = str(script_runs_dir.resolve())

    base_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (
            workspace_pythonpath,
            existing_pythonpath,
        ) if part
    )
    log_lock = threading.Lock()
    if chrome_path_used:
        rr = RunResult(
            feature="(orchestrator)",
            status="success",
            message="ChromeDriver resolved and passed to subprocess environment",
            started_at=datetime.now(),
            duration_sec=None,
            duration_hms="",
            sqlite_copied=False,
            timestamp=datetime.now(),
            stderr=None,
            session_id=session_id,
            correlation_id=correlation_id,
        )
        _write_log_line(log_lock, logs_dir, rr)
        _emit_structured_event(
            logs_dir,
            "worker_initialized",
            correlation_id=correlation_id,
            session_id=session_id,
            chromedriver_resolved=True,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:

        future_to_feature = {}
        for f in features:
            if cancel_event is not None and cancel_event.is_set():
                break
            per_timeout = timeout
            if feature_timeout_map is not None:
                t = feature_timeout_map.get(str(f).strip())
                if isinstance(t, int) and t > 0:
                    per_timeout = t

            future_to_feature[executor.submit(
                _run_single_feature, f, script_runs_dir, per_timeout, base_env, history_path, session_id, correlation_id, resolved_python[f],
            )] = f



        for future in as_completed(future_to_feature):
            if cancel_event is not None and cancel_event.is_set():
                # Do not block waiting for remaining tasks
                break
            try:
                result = future.result()
            except Exception as e:
                feature = future_to_feature[future]
                result = RunResult(
                    feature=feature,
                    status="failure",
                    message=f"Unhandled exception in runner: {e}",
                    started_at=datetime.now(),
                    duration_sec=None,
                    duration_hms="",
                    sqlite_copied=False,
                    timestamp=None,
                    stderr=None,
                    session_id=session_id,
                    correlation_id=correlation_id,
                )
            if workflow_status_map is not None:
                result.workflow_status = workflow_status_map.get(result.feature, "")

            metadata = {}
            if feature_metadata_map is not None:
                metadata = feature_metadata_map.get(result.feature, {})

            result.frequency = str(metadata.get("frequency", "") or "").strip()
            result.bugs = str(metadata.get("bugs", "") or "").strip()
            result.notes = str(metadata.get("notes", "") or "").strip()

            try:
                timeout_used = int(timeout)
            except Exception:
                timeout_used = DEFAULT_TIMEOUT

            if timeout_used <= 0:
                timeout_used = DEFAULT_TIMEOUT

            if feature_timeout_map is not None:
                tv = feature_timeout_map.get(result.feature)
                if isinstance(tv, int) and tv > 0:
                    timeout_used = int(tv)

            result.timeout_sec_used = timeout_used

            _append_run_history(history_path, {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "feature_number": result.feature,
                "Status": result.workflow_status or "",
                "action": "Run",
                "result": result.status.lower(),
                "duration_sec": result.duration_sec,
                "timeout_sec_used": timeout_used,
                "sqlite_copied": bool(result.sqlite_copied),
                "session_id": result.session_id or session_id,
                "correlation_id": result.correlation_id or correlation_id,
                "message": result.message or "",
            })

            results.append(result)

            _write_log_line(log_lock, logs_dir, result)
            _write_csv_row(log_lock, logs_dir, result)

            _emit_structured_event(
                logs_dir,
                "run_result",
                correlation_id=result.correlation_id or correlation_id,
                session_id=result.session_id or session_id,
                feature_number=result.feature,
                workflow_status=result.workflow_status or "",
                result=result.status.lower(),
                duration_sec=result.duration_sec,
                timeout_sec_used=timeout_used,
                sqlite_copied=bool(result.sqlite_copied),
                message=result.message or "",
            )

            if progress_callback is not None:
                try:
                    progress_callback(result)
                except Exception:
                    pass

    return results
