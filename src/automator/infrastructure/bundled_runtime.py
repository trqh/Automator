"""Launch installed Automator helpers with a separate bundled Python runtime.

A frozen GUI executable is not a general Python interpreter. The release
must contain a working interpreter under runtime/ with the Automator wheel
and its required dependencies installed for ClickUp helper execution.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from .runtime_config import RuntimeSettingsError, probe_script_python


def runtime_python(*, frozen: Optional[bool] = None,
                   gui_executable: Optional[Path] = None,
                   platform: Optional[str] = None) -> Path:
    """Resolve the helper interpreter, independent of user script settings.

    Development uses the running Python; frozen builds use runtime/ installed
    next to the GUI. The optional override is for packaging verification.
    """
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    current = Path(sys.executable if gui_executable is None else gui_executable).resolve()
    if not is_frozen:
        return current
    override = os.environ.get("AUTOMATOR_BUNDLED_PYTHON", "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
    else:
        os_name = sys.platform if platform is None else platform
        subpath = ("runtime/python.exe" if os_name == "win32"
                   else "runtime/bin/python3")
        candidate = current.parent / subpath
    if not candidate.is_file():
        raise RuntimeSettingsError(
            f"Bundled Python was not found: {candidate}. "
            "The application distribution is incomplete."
        )
    if candidate == current:
        raise RuntimeSettingsError("The GUI executable cannot be used as script Python")
    return candidate


def helper_command(module: str, *, frozen: Optional[bool] = None,
                   gui_executable: Optional[Path] = None,
                   platform: Optional[str] = None) -> list[str]:
    """Build a package-module command; never launch a root source .py file."""
    if not module.startswith("automator.") and not module.startswith("src.automator."):
        raise ValueError("Automator helper must be an Automator package module")
    return [str(runtime_python(frozen=frozen, gui_executable=gui_executable,
                               platform=platform)), "-m", module]


def verify_bundled_runtime(*, frozen: Optional[bool] = None,
                           gui_executable: Optional[Path] = None,
                           platform: Optional[str] = None) -> str:
    """Validate interpreter before distribution; no network or data changes."""
    python = runtime_python(frozen=frozen, gui_executable=gui_executable,
                            platform=platform)
    return probe_script_python(python)
