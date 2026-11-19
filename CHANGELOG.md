# Changelog

All notable released changes are recorded here. Dates reflect project verification, not prior public availability.

## [0.1.0] — Initial public release candidate (2026-11-18)

- Desktop GUI for Python feature-script workflows and run history.
- Optional ClickUp integration; generic `WorkflowStatus` naming replaces the previous field name.
- First-run creation of user records and workspace with no overwrite of existing records.
- Configurable interpreter selection and separated per-user application storage.
- Built-in offline Python demo and synthetic SQLite comparison demonstration.
- Explicit canonical SQLite acceptance and collision-safe canonical history.
- Windows x64 one-folder packaging build and bundled helper Python.
- Verified 195 tests on Windows (source stage and development) and Ubuntu 26.04.1 WSL2; actual Ubuntu GUI and both demos verified.

### Planned (not part of v0.1.0)

- GUI-managed workflow statuses, ClickUp status mappings, and ClickUp custom-field selection.
- Additional native OS distribution options after platform-specific verification.
