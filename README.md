# Automator

**Free, open-source desktop application for organizing, running, and reviewing Python feature scripts.**

Automator is a Tkinter desktop application for feature workspaces, script execution, logs, optional ClickUp workflows, and SQLite comparison with explicit canonical approval and history.

**License:** MIT · **Version:** 0.1.1 · **Python:** 3.10+ (source installation)

## What changed in v0.1.1

- First-run setup installs **Feature 900003 — Fictional People Dataset** into the normal feature workspace and registers it in `records.json`.
- Its offline SQLite output uses fictional `person` and `identifiers` records; no account, credentials, or network access is needed.
- The two obsolete demo controls and their legacy modules were removed.
- Python interpreter validation reports probe duration and errors without blocking the settings dialog.
- The Windows portable build includes a Python runtime for the *trusted, unmodified* example and its documented `BASELINE_ONLY` toggle. Other scripts require a separately selected interpreter.
- Existing records, example scripts, settings, and canonical history are not silently overwritten.

## Quick start — Feature 900003

1. Download the Windows x64 release ZIP when published and extract **all** its files, including `_internal/` and `runtime/`; launch `Automator.exe`.
2. In a fresh user-data directory, locate **900003** in the feature list. Its setup status should be **READY**.
3. Select it and click **Run selected**. The feature creates `feature_900003/900003.sqlite` in the configured Script Runs directory.
4. Select **SQLite → Compare selected**. The first comparison should report **NO CANONICAL**. Review the candidate and explicitly accept it as the canonical database.
5. Compare again to see **SAME**.
6. In the **user-data feature workspace**, edit `feature_900003/main.py` and change `BASELINE_ONLY = True` to `BASELINE_ONLY = False`.
7. Rerun, compare (**DIFFERENT**, including fictional identifier `DEMO-004`), accept the updated canonical, and compare again (**SAME**).
8. The earlier approved database is retained under `Canonical Output History/900003/`.

All names and identifiers in this example are fabricated. Do not edit the copy under the installed application package. Other changes to the example's source beyond its documented boolean toggle require an external interpreter; Automator does **not** sandbox Python scripts.

## Main-window controls

The following labels are drawn from the application GUI. Some controls are enabled only for particular selections, work states, or optional ClickUp configuration.

| Control | Purpose |
|---|---|
| **Script Runs dir → Browse...** | Choose the workspace containing `feature_<number>/main.py` folders. |
| **Max workers** | Set the concurrency limit for applicable batch operations. |
| **Run selected** | Execute selected registered feature scripts using the normal worker. |
| **Rerun failed** | Retry features marked as failed by the run workflow. |
| **Pack selected** | Package selected features through the existing pack worker. |
| **Copy packed zips** | Copy previously produced feature ZIP packages using the copy worker. |
| **Batch outputs for selected** | Batch-process outputs for selected features. |
| **Validate selected** | Run validation for selected feature scripts. |
| **Clean directories** | Run the selected-feature directory cleanup worker; inspect what it will remove before use. |
| **Sync data from clickup** | Synchronize records from configured ClickUp integration; optional and not needed for the built-in example. |
| **Python Settings** | Select and validate the interpreter used by ordinary user scripts. |
| **Reload** | Refresh records and the visible GUI state. |
| **Select all / Select none / Invert** | Change selection for the currently filtered list of features. |
| **Columns ▼** | Choose the table columns displayed in the current view. |
| **Copy diagnostics** | Copy diagnostic information for troubleshooting. |
| **Cancel / Hard Cancel Now** | Request cancellation of an active operation; hard cancellation is more forceful and should be reserved for a stuck operation. |

### Work menu

| Menu item | Purpose |
|---|---|
| **Start Work / Stop Work** | Start or stop time tracking for the selected feature. |
| **Initialize selected** | Create missing feature setup files for selected registered features. |
| **Start Development** | Start the development workflow for a selected feature. |
| **Review Work Ledger** | View recorded work history and time information. |
| **Manage Work Categories** | Manage categories used by the work-tracking workflow. |

### SQLite and ClickUp menus

| Menu item | Purpose |
|---|---|
| **SQLite → Compare selected** | Compare a selected candidate SQLite database against its approved canonical. Approval is a separate explicit action. |
| **ClickUp → Update tasks in clickup** | Send applicable task updates to the configured ClickUp workspace. |
| **ClickUp → End Workday / Sync ClickUp** | Review or execute the end-of-workday synchronization workflow. |
| **ClickUp → Weekly reconciliation** | Open the reconciliation workflow where enabled. |

ClickUp actions require user-provided configuration and appropriate access; they are not part of the offline demonstration. There are **no** separate **Run Demo** or **Run SQLite Comparison Demo** controls in v0.1.1. Individual dialog actions (for example, approval confirmation and Python validation) are explained in the applicable workflow above or in [INSTALLATION.md](INSTALLATION.md).

## Platform support and verification

| Platform | Verified status |
|---|---|
| Windows 11 x64 | v0.1.1: 208 regression tests passed; packaged executable and all 12 Feature 900003 acceptance checks passed on the tested Windows host. |
| Ubuntu 26.04.1 under WSL 2 | v0.1.0 source and GUI were tested previously; **v0.1.1 Linux GUI acceptance has not been reported**. |
| Other Linux distributions | Not tested for v0.1.1. |
| macOS | Not tested; no native installer. |

The Windows build is a **one-folder distribution**, not a standalone `.exe` file. A clean isolated user-data run was verified on the development Windows host; it was not a separate clean virtual machine.

## Data, privacy, and security

On Windows, application data defaults to `%APPDATA%\Automator`; feature workspaces default under that root. `AUTOMATOR_DATA_ROOT`, `AUTOMATOR_WORKSPACE_DIR`, and `AUTOMATOR_SCRIPT_PYTHON` can override their respective settings. Do not commit credentials, `.env`, user records, logs, or SQLite data.

Python scripts execute with the current user's privileges. Only run scripts you trust. A trusted bundled-example allowlist recognizes the shipped Feature 900003 script and its documented boolean toggle, but it is **not** a security sandbox for arbitrary scripts. The simulated symlink rejection test passed, and a separate real Windows symbolic-link test confirmed that the bundled-runtime allowlist rejects symlinked example scripts.

ClickUp integration is optional. The default field convention uses `WorkflowStatus`; user-configurable status mappings are not included in v0.1.1.

## Development and documentation

To install the source, use Python 3.10 or newer and follow [INSTALLATION.md](INSTALLATION.md). The v0.1.1 Windows suite has 208 passing tests (no skipped tests on the reported run); this result does not guarantee behavior on every system.

- [Installation and Windows build](INSTALLATION.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Contributing](CONTRIBUTING.md)
- [Security and disclosure](SECURITY.md)
- [Third-party licenses and notices](THIRD_PARTY_NOTICES.md)
- [Release acceptance checklist](RELEASE_CHECKLIST.md)

## License

Copyright (c) 2026 Tareq Hamarneh. Automator's original code is licensed under the [MIT License](LICENSE). Bundled third-party components retain their own licenses.
