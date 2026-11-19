# Changelog

All notable released changes are recorded here. Dates reflect project verification, not prior public availability.

## [0.1.1] — Release candidate (verified 2026-11-20; not yet published)

- Added Feature 900003, an offline deterministic fictional-people SQLite example registered automatically on first run.
- Supports the baseline/fourth-person `BASELINE_ONLY` toggle through the normal feature worker and bundled Windows Python, with explicit trusted-example source checks.
- Verified candidate SQLite comparison, explicit canonical acceptance, change detection, canonical-history archival, and relaunch persistence.
- Removed separate Run Demo and SQLite Comparison Demo controls, legacy Feature 900001 resources, and obsolete demo services.
- Improved interpreter validation diagnostics, elapsed-time reporting, and Tk GUI responsiveness; Python 3.9.7 probe measured at 0.123 seconds on the tested host.
- Fixed SQLite self-copy handling for a candidate already in its destination directory.
- Windows regression: 208 passed, 0 skipped; frozen Windows GUI: 12/12 acceptance checks passed.
- Automated candidate scan and independent Gitleaks Git/source/tests/scripts reports showed zero findings; 22 upstream license sources collected.
- Symlink rejection was verified with both the security-test simulated fallback and a separate real Windows symbolic link, which the bundled-runtime allowlist rejected.
- v0.1.1 documentation and final release packaging/publication checks remain subject to final acceptance.

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
