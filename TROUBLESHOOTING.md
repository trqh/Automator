# Troubleshooting

## The Windows `Automator.exe` does not launch

Extract the complete release directory; do not move the `.exe` away from `_internal/` and `runtime/`. Check Windows security prompts and use an official release asset. The portable distribution is Windows x64 only.

## The Linux GUI does not appear

Make sure Python Tk is installed (`python3-tk`) and a graphical display is available. On Windows Subsystem for Linux, verify WSLg by opening a small `tkinter.Tk()` test window. For test automation without a display, install `xvfb` and use `xvfb-run -a`.

## `records.json` or `records_meta.json` missing

First launch should create missing files under `<data-root>/data/`. Make sure the storage directory is writable. `AUTOMATOR_DATA_ROOT` must point to the **root**, not to its `data/` subdirectory. Do not delete existing production records to repair an installation.

## A custom script cannot run but the demo works

The Windows portable helper Python is intended for the built-in offline demo. Select a suitable external Python interpreter in the GUI for arbitrary scripts; install the script's required packages in that environment. Check workspace selection and logs.

## SQLite shows `NO CANONICAL`

The first candidate is not approved automatically. Review and explicitly accept it; previous canonical versions are preserved under `Canonical Output History`. Back up canonical outputs when moving installations.

## SQLite acceptance says access denied on Windows

Close external SQLite/database viewers and retry after confirming file handles are released. Do not delete canonical history to bypass a lock.

## ClickUp status/field configuration does not match

ClickUp is optional. Version 0.1.0 uses a default workflow-field convention (`WorkflowStatus`); custom configuration/mapping is **planned, not implemented**. Ensure your own ClickUp configuration matches expected fields and statuses before synchronizing. Supply your own credentials and keep them outside version control.

## Python `ModuleNotFoundError` in source or tests

Create a virtual environment, install the project (`python -m pip install -e .`), install `pytest`, and run tests from the repository root. Do not rely on old checkout-level import shims.

## Reporting issues

Report OS, exact Python version, relevant action, and a **synthetic** reproducer. Scrub credentials, ClickUp identifiers, company data, SQLite contents, and sensitive paths from screenshots and logs.
