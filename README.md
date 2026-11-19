# Automator

**Free, open-source desktop application for organizing, running, and reviewing Python feature scripts.**

Automator provides a Tkinter-based GUI for feature workspaces, Python interpreter selection, script execution, logs, optional ClickUp workflows, and comparison of candidate SQLite outputs with approved canonical databases.

**License:** MIT · **Version:** 0.1.0 · **Python:** 3.10+ (source install)

## Features

- Organize Python feature scripts and review execution history.
- Select the workspace and Python interpreter used for scripts.
- Run an offline example feature without a ClickUp account.
- Compare candidate SQLite outputs to approved canonical outputs; accept a candidate explicitly and preserve canonical history.
- Optionally integrate with ClickUp using credentials and configuration provided by the user.
- Initialize missing records, metadata, and canonical directories without overwriting existing data.

## Platform support

| Platform | Status |
|---|---|
| Windows 11, x64 | Source test suite and standalone portable build previously verified; final v0.1.0 rebuild/clean-install acceptance required |
| Ubuntu 26.04.1 LTS (WSL 2) | Source installed; 195 tests passed; actual GUI, offline demo and SQLite comparison demo worked |
| Other Linux distributions | Not tested |
| macOS | Source reviewed for portability but not tested; no native package |

The Windows **portable distribution** is a complete extracted folder, **not just `Automator.exe`**. If no release binary is attached yet, use the source installation method.

## Quick start — source

```sh
python -m venv .venv
# Windows CMD: .venv\Scripts\python.exe -m pip install -e .
# Linux: .venv/bin/python -m pip install -e .
```

Run the corresponding virtual environment's Python with `-m automator.ui.feature_manager_gui` to launch the app. See [INSTALLATION.md](INSTALLATION.md) for complete commands and platform prerequisites.

## Data and privacy

User data is stored outside the installed package, by default under `%APPDATA%\Automator` (Windows), `${XDG_DATA_HOME:-~/.local/share}/automator` (Linux), or `~/Library/Application Support/Automator` (macOS, untested).

`AUTOMATOR_DATA_ROOT` overrides the **storage root** (parent of `data/`, `logs/`, canonical directories); `AUTOMATOR_WORKSPACE_DIR` overrides the feature workspace; `AUTOMATOR_SCRIPT_PYTHON` selects a script interpreter. Never commit API credentials, `.env`, logs, real records, or SQLite datasets.

ClickUp is optional. **WorkflowStatus** is currently the default ClickUp custom-field nomenclature. Configurable workflow/status mapping is proposed for a future version, **not included in v0.1.0**. Users must match their own ClickUp configuration before running synchronization; do not assume it will map arbitrary workspaces automatically.

## Development and support

Run `python -m pip install -e . pytest` then `python -m pytest -q tests` inside your virtual environment. The v0.1.0 test suite contains 195 tests; test results are environment-specific, not promises for every OS.

- [Installation and Windows build](INSTALLATION.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Contributing](CONTRIBUTING.md)
- [Security and disclosure](SECURITY.md)
- [Third-party licenses and notices](THIRD_PARTY_NOTICES.md)
- [Release acceptance checklist](RELEASE_CHECKLIST.md)

## License

Copyright (c) 2026 Tareq Hamarneh. Automator's original code is licensed under the [MIT License](LICENSE). You may use, copy, modify, and redistribute it—including commercially—subject to the license's notice conditions. Bundled third-party components retain their respective licenses.
