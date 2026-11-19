"""Restrict bundled Python to the exact two packaged example script variants.

Feature numbers are user-controlled and are NOT authorization. The bundled
interpreter is used only when the editable main.py is byte-for-byte equal to
our published baseline or to the same file with the documented boolean toggle.
Ordinary Python scripts still require an external interpreter in frozen builds.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

EXAMPLE_ID = "900003"
EXAMPLE_PACKAGE = "automator.demo_scripts.feature_900003"
TOGGLE_TRUE = b"BASELINE_ONLY = True"
TOGGLE_FALSE = b"BASELINE_ONLY = False"


def is_trusted_example(feature: str, workspace: Path) -> bool:
    """Check installed source against published resource; deny on any uncertainty."""
    if str(feature).strip() != EXAMPLE_ID:
        return False
    folder = Path(workspace) / f"feature_{EXAMPLE_ID}"
    script = folder / "main.py"
    try:
        # No symlinks in any relative component; prevent referencing other code.
        if folder.is_symlink() or script.is_symlink() or not script.is_file():
            return False
        baseline = files(EXAMPLE_PACKAGE).joinpath("main.py").read_bytes()
        if baseline.count(TOGGLE_TRUE) != 1:
            return False
        candidate = script.read_bytes()
        return candidate in (baseline, baseline.replace(TOGGLE_TRUE, TOGGLE_FALSE, 1))
    except (OSError, ValueError, ModuleNotFoundError, TypeError):
        return False
