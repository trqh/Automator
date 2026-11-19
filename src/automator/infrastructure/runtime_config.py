"""Portable Automator runtime locations and external script-Python preferences.

This module never uses the installed package directory as writable storage.
Resolving and reading settings do not create files or directories.
I12 only establishes the contract; GUI/worker wiring follows in I13-I14.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .storage_paths import StoragePaths, legacy_storage

DATA_ROOT_ENV = "AUTOMATOR_DATA_ROOT"
WORKSPACE_ENV = "AUTOMATOR_WORKSPACE_DIR"
SCRIPT_PYTHON_ENV = "AUTOMATOR_SCRIPT_PYTHON"
SETTINGS_NAME = "runtime_settings.json"


class RuntimeSettingsError(ValueError):
    """An invalid runtime setting or script interpreter was supplied."""


def _absolute_path(value: str | os.PathLike[str], description: str) -> Path:
    text = str(value).strip()
    if not text:
        raise RuntimeSettingsError(f"{description} cannot be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise RuntimeSettingsError(f"{description} must be an absolute path: {text}")
    return path.resolve()


def default_data_root(*, environment: Optional[Mapping[str, str]] = None,
                      platform: Optional[str] = None, home: Optional[Path] = None) -> Path:
    """Choose per-user writable application storage without filesystem mutations."""
    env = os.environ if environment is None else environment
    plat = sys.platform if platform is None else platform
    user_home = Path.home() if home is None else Path(home)
    if plat == "win32":
        base = env.get("APPDATA") or str(user_home / "AppData" / "Roaming")
        return _absolute_path(base, "Windows app data directory") / "Automator"
    if plat == "darwin":
        return (user_home / "Library" / "Application Support" / "Automator").resolve()
    base = env.get("XDG_DATA_HOME") or str(user_home / ".local" / "share")
    return _absolute_path(base, "XDG data directory") / "automator"


@dataclass(frozen=True)
class RuntimeSettings:
    """Resolved runtime preferences; no mutation of legacy data or package source."""

    data_root: Path
    workspace_dir: Optional[Path] = None
    script_python: Optional[Path] = None

    @property
    def storage(self) -> StoragePaths:
        return legacy_storage(self.data_root)

    @property
    def settings_file(self) -> Path:
        return self.data_root / "data" / SETTINGS_NAME


def load_runtime_settings(*, legacy_app_root: Optional[Path] = None,
                          environment: Optional[Mapping[str, str]] = None,
                          platform: Optional[str] = None,
                          home: Optional[Path] = None) -> RuntimeSettings:
    """Read preferences with explicit precedence: environment > saved > defaults.

    Pass legacy_app_root only for the existing developer installation; a clean
    installed application omits it and receives a per-user data directory.
    """
    env = os.environ if environment is None else environment
    if env.get(DATA_ROOT_ENV):
        root = _absolute_path(env[DATA_ROOT_ENV], DATA_ROOT_ENV)
    elif legacy_app_root is not None:
        root = _absolute_path(legacy_app_root, "legacy_app_root")
    else:
        root = default_data_root(environment=env, platform=platform, home=home)
    settings_file = root / "data" / SETTINGS_NAME
    stored: dict[str, object] = {}
    if settings_file.is_file():
        try:
            loaded = json.loads(settings_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeSettingsError(f"Cannot read runtime settings: {settings_file}") from exc
        if not isinstance(loaded, dict) or loaded.get("version") != 1:
            raise RuntimeSettingsError("Unsupported runtime settings schema")
        stored = loaded

    def optional_path(key: str, override: str, default: Optional[Path] = None) -> Optional[Path]:
        raw = env.get(override) or stored.get(key)
        if raw is None or raw == "":
            return default
        if not isinstance(raw, str):
            raise RuntimeSettingsError(f"Invalid {key} in runtime settings")
        return _absolute_path(raw, key)

    legacy_workspace = _absolute_path(legacy_app_root, "legacy_app_root").parent if legacy_app_root is not None else None
    return RuntimeSettings(
        data_root=root,
        workspace_dir=optional_path("workspace_dir", WORKSPACE_ENV, legacy_workspace),
        script_python=optional_path("script_python", SCRIPT_PYTHON_ENV),
    )


def save_runtime_settings(settings: RuntimeSettings) -> Path:
    """Atomically save preferences; never move or rewrite existing records/logs."""
    root = _absolute_path(settings.data_root, "data_root")
    payload = {
        "version": 1,
        "workspace_dir": str(_absolute_path(settings.workspace_dir, "workspace_dir"))
        if settings.workspace_dir is not None else None,
        "script_python": str(_absolute_path(settings.script_python, "script_python"))
        if settings.script_python is not None else None,
    }
    destination = root / "data" / SETTINGS_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=SETTINGS_NAME + ".", suffix=".tmp",
                                    dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def probe_script_python_timed(executable: Path, *, timeout: float = 30.0) -> tuple[str, float]:
    """Return (version, seconds) from an external Python probe.

    Preserve errors with timing, underlying exception detail, and stderr.
    No environment mutation or global DLL path changes are performed.
    """
    import time

    python = _absolute_path(executable, "script_python")
    if not python.is_file():
        raise RuntimeSettingsError(f"Python executable does not exist: {python}")
    start = time.perf_counter()
    try:
        completed = subprocess.run(
            [str(python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        elapsed = time.perf_counter() - start
        raise RuntimeSettingsError(
            f"Cannot launch script Python: {python}\n"
            f"Error type: {type(exc).__name__}\n"
            f"Elapsed: {elapsed:.3f}s; timeout: {timeout:.1f}s\n"
            f"Details: {exc}"
        ) from exc
    elapsed = time.perf_counter() - start
    version = completed.stdout.strip()
    valid = (completed.returncode == 0 and len(version.split(".")) == 3
             and all(p.isdigit() for p in version.split(".")))
    if not valid:
        stderr = (completed.stderr or "").strip()[:800]
        raise RuntimeSettingsError(
            f"Selected executable is not a usable Python interpreter: {python}\n"
            f"Exit code: {completed.returncode}; elapsed: {elapsed:.3f}s\n"
            f"Elapsed: {elapsed:.3f}s\n"
            f"stderr: {stderr or '(empty)'}"
        )
    return version, elapsed


def probe_script_python(executable: Path, *, timeout: float = 30.0) -> str:
    """Backward-compatible version-only probe."""
    version, _ = probe_script_python_timed(executable, timeout=timeout)
    return version


def selected_script_python(settings: RuntimeSettings, *, frozen: Optional[bool] = None,
                           development_python: Optional[Path] = None) -> Path:
    """Choose the interpreter for user scripts, never a frozen GUI executable.

    During development, maintain today's sys.executable behavior until I13
    introduces explicit choice in the worker; frozen applications require a
    separately selected interpreter and never run scripts using Automator.exe.
    """
    if settings.script_python is not None:
        return _absolute_path(settings.script_python, "script_python")
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        raise RuntimeSettingsError("Select an external Python interpreter to run scripts")
    return _absolute_path(development_python or sys.executable, "development_python")


def resolve_feature_interpreter(
    settings: RuntimeSettings,
    *,
    is_demo: bool = False,
    bundled_python: Optional[Path] = None,
    frozen: Optional[bool] = None,
    development_python: Optional[Path] = None,
) -> Path:
    """Choose Python separately from the Automator GUI process.

    Explicit user selections always take precedence. Bundled Python is only
    the automatic fallback for trusted, bundled demonstration scripts.
    A frozen GUI executable must never be used as a Python interpreter.
    """
    if settings.script_python is not None:
        return _absolute_path(settings.script_python, "script_python")
    if is_demo and bundled_python is not None:
        candidate = _absolute_path(bundled_python, "bundled_python")
        if not candidate.is_file():
            raise RuntimeSettingsError(f"Bundled demonstration Python not found: {candidate}")
        return candidate
    return selected_script_python(
        settings, frozen=frozen, development_python=development_python,
    )


# I13B: Runtime locations for packaged workers. Source-tree detection is a
# development-only bridge; it never treats an installed wheel as writable data.
def worker_runtime_settings(module_file: str | os.PathLike[str]) -> RuntimeSettings:
    """Load runtime settings, preserving the checkout's original data/workspace.

    Explicit AUTOMATOR_DATA_ROOT overrides the developer checkout data root.
    A wheel/standalone installation uses the user's platform data directory.
    No directories are created by this function.
    """
    module = Path(module_file).resolve()
    legacy_root = None
    for parent in module.parents:
        if (parent / "pyproject.toml").is_file() and (
            parent / "src" / "automator" / "infrastructure" / "runtime_config.py"
        ).is_file():
            legacy_root = parent
            break
    return load_runtime_settings(legacy_app_root=legacy_root)


def worker_workspace(settings: RuntimeSettings) -> Path:
    """Return a user-writable feature directory, never the package directory."""
    return (settings.workspace_dir or (settings.data_root / "features")).resolve()
