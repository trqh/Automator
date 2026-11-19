# Installation and build instructions — Automator v0.1.1

## Windows x64 portable distribution

1. Download the official v0.1.1 Windows release archive **once published** and verify the published checksum.
2. Extract the *entire* archive to a writable directory. Keep `Automator.exe`, `_internal/`, and `runtime/` together.
3. Launch `Automator.exe`. On an empty application-data directory, Feature **900003** is registered automatically and shows **READY**.
4. Select 900003, click **Run selected**, then use **SQLite → Compare selected** to establish and compare an explicitly approved baseline.

No system Python, ClickUp credentials, or network connection is needed to run the trusted bundled example in the Windows portable app. Its packaged runtime is **not** automatically available to arbitrary or modified user scripts. For normal scripts and their third-party dependencies, choose a suitable external interpreter in **Python Settings**. The documented `BASELINE_ONLY` boolean toggle is supported for the bundled example.

A Windows 11 x64 frozen build passed a source-host clean-data GUI workflow, including `NO CANONICAL → SAME → DIFFERENT → SAME`, approval history, relaunch persistence, and offline execution. This was not a separate clean Windows VM test.

## Windows source installation (Python 3.10+)

Use Windows CMD from the checked-out repository root:

```cmd
cd /d "C:\path\to\Automator"
py -3.14 -m venv .venv
".venv\Scripts\python.exe" -m pip install -e . pytest
if not exist "C:\Automator_Pytest_Temp" mkdir "C:\Automator_Pytest_Temp"
".venv\Scripts\python.exe" -m pytest -q --basetemp="C:\Automator_Pytest_Temp\pytest"
".venv\Scripts\python.exe" -m automator.ui.feature_manager_gui
```

Python 3.14 is illustrative; the package declares Python 3.10+. For the project's verified Windows staging tests, preserve its explicit `TEMP`, `TMP`, and `PYTHONPATH` test environment when required.

## Ubuntu/Linux source installation

The v0.1.0 source test suite and GUI ran on Ubuntu 26.04.1 under WSL 2 (Python 3.14.4). **v0.1.1 Linux acceptance has not been verified.** Linux users need Python, Tkinter, and a graphical display. No Linux native installer is included. Refer to platform-specific setup instructions for Python packaging and Tk installation.

## Windows release build (maintainers)

The Windows builder requires Python 3.14, its release environment/dependencies, PowerShell available to the builder, and the **official** Python 3.14.2 Windows x64 embeddable ZIP from python.org. Verify its provenance. Invoke from CMD, providing the actual ZIP path:

```cmd
cd /d "C:\path\to\Automator"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\build_windows_release.ps1" -EmbedZip "C:\path\to\python-3.14.2-embed-amd64.zip"
```

The builder produces a wheel, PyInstaller one-folder GUI, and helper runtime in `dist/Automator/`. It performs static checks but **does not replace** interactive GUI acceptance, secret review, final license compliance, or release checksums. Rebuild and reverify whenever source code or shipped dependencies change.

## Isolated Windows acceptance (CMD)

From the staging directory, use a **new, unused** directory (never delete real user data):

```cmd
cd /d "C:\path\to\Automator"
set "AUTOMATOR_DATA_ROOT=C:\AutomatorSmokeData_New"
set "AUTOMATOR_WORKSPACE_DIR=C:\AutomatorSmokeData_New\features"
set "AUTOMATOR_SCRIPT_PYTHON="
"dist\Automator\Automator.exe"
```

Check that 900003 is `READY`, runs without external Python, and completes canonical acceptance and change detection. Edit only the user-workspace `feature_900003/main.py` to change `BASELINE_ONLY` from `True` to `False`, then rerun and compare. Check `Canonical Outputs/900003.sqlite` and `Canonical Output History/900003/` before and after reopening the GUI.

## Storage overrides

`AUTOMATOR_DATA_ROOT` sets the application-data **root**, not its `data/` child. `AUTOMATOR_WORKSPACE_DIR` sets the feature workspace. `AUTOMATOR_SCRIPT_PYTHON` optionally selects an interpreter. On Windows, application data defaults to `%APPDATA%\Automator` and the feature workspace defaults to its `features` folder. Existing `records.json`, settings, scripts, and canonical files are not silently overwritten; back them up before moving or changing storage locations.
