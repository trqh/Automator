# Installation and build instructions

## Windows x64 portable distribution

1. Download a published Windows x64 release asset from this project's GitHub Releases page (when available).
2. Extract the **entire** archive to a writable location.
3. Launch `Automator.exe` from the extracted `Automator` folder. Keep `_internal/` and `runtime/` beside it.
4. Run the built-in offline feature demo first. ClickUp credentials are **not** necessary.

The bundled Python runtime supports the trusted built-in demo. For your own scripts and their third-party packages, use Python Settings to select an external interpreter. The release asset must be verified on Windows before it is published; the mere existence of a build artifact is insufficient.

## Windows source installation (Python 3.10+)

Windows CMD, from the checked-out repository root:

```cmd
cd /d "C:\path\to\Automator"
py -3.14 -m venv .venv
".venv\Scripts\python.exe" -m pip install -e . pytest
".venv\Scripts\python.exe" -m pytest -q tests
".venv\Scripts\python.exe" -m automator.ui.feature_manager_gui
```

Python 3.14 is an example used for validation; the package declares Python >=3.10. Install a compatible version if the `py -3.14` launcher is unavailable.

## Ubuntu/Linux source installation

The source application was verified on Ubuntu 26.04.1 LTS under WSL 2 using Python 3.14.4. Other distributions may require different package names. Run the following **inside Ubuntu**:

```sh
sudo apt update
sudo apt install -y python3-venv python3-pip python3-tk xvfb xauth
cd /path/to/Automator
python3 -m venv .venv
.venv/bin/python -m pip install -e . pytest
xvfb-run -a .venv/bin/python -m pytest -q tests
.venv/bin/python -m automator.ui.feature_manager_gui
```

A graphical display server is required for the GUI (for WSL, WSLg). `xvfb-run` is for headless GUI tests; launch the normal GUI without it when an actual display is available. On Ubuntu WSL2 the 195-test suite and the real GUI, offline demo, and SQLite comparison demo were verified. No Linux native installer or macOS executable is included.

## Building the Windows x64 portable release (maintainers)

You need Windows x64, Python 3.14, PowerShell, network access for build dependencies, and the **official** Python 3.14 Windows x64 embeddable ZIP from python.org. Verify the download independently and use the matching filename. Do not build as administrator.

```cmd
cd /d "C:\path\to\Automator"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows_release.ps1 -EmbedZip "%TEMP%\python-3.14.2-embed-amd64.zip"
```

The builder generates a Python wheel, a PyInstaller one-folder GUI, and a bundled Python helper runtime at `dist\Automator\`. It runs a static distribution check, but **does not** perform a clean-machine GUI acceptance test. Inspect the actual artifact and include required upstream copyright/license notices and CPython/PyInstaller license obligations before redistributing it. Rebuild after every source or dependency change.

## Storage overrides

Use a separate writable location when testing. On Windows CMD:

```cmd
cd /d "C:\path\to\Automator"
set "AUTOMATOR_DATA_ROOT=C:\AutomatorSmokeData"
set "AUTOMATOR_WORKSPACE_DIR=C:\AutomatorSmokeData\features"
Automator.exe
```

On Ubuntu:

```sh
export AUTOMATOR_DATA_ROOT="$HOME/automator-smoke-data"
export AUTOMATOR_WORKSPACE_DIR="$HOME/automator-smoke-data/features"
```

`AUTOMATOR_DATA_ROOT` is the **root**, not its `data/` subfolder. First run creates missing `data/records.json` and `data/records_meta.json`, preserving existing files. For production use, back up records and canonical outputs before changing storage locations.
