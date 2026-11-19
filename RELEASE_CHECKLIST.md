# Automator v0.1.0 — Release acceptance checklist

**This is a release gate.** Checked items represent reported verification; remaining unchecked items must be completed before public source publication or binary upload. Do not publish a Windows artifact based solely on a successful build.

## Completed and reported

- [x] Author declares original authorship and control over publication of source code.
- [x] Source was staged by allowlist, separately from operational D: data; no old Git history was intentionally copied.
- [x] Public source audit found no obvious hard-coded credentials in reviewed source; metadata inventory completed.
- [x] the previous field name literal references renamed to generic `WorkflowStatus` in four files; staged 195-test suite passed afterward.
- [x] Staged test imports migrated to package paths; synchronization to development performed with backups.
- [x] 195 tests and compilation succeeded in Windows staging and Windows development.
- [x] 195 tests passed under Ubuntu 26.04.1 WSL2; Linux GUI displayed; offline and SQLite comparison demos behaved as expected.
- [x] Python dependency metadata audit returned `pip check` success; MIT chosen for Automator's original code.

## Complete before publishing repository

- [ ] Apply refreshed public docs, `LICENSE`, and `.gitignore` to the **C: staging root only**.
- [ ] Make sure C: and D: contain the same verified `WorkflowStatus` application/test changes; do not transfer operational data.
- [ ] Remove/move the staging-only `NOT_READY_FOR_PUBLICATION.txt`, `I16A_STAGING_MANIFEST.json`, and generated `src/automator.egg-info/` from the eventual Git tracked inventory. Confirm ignored logs, data, build artifacts, environments, and credentials cannot be staged.
- [ ] Inspect final Git **staged** file list, `git diff --cached --check`, and diff content before first commit; use a *fresh* Git history (never import the D: `.git`).
- [ ] Run final 195-test suites after applying documentation and license, verify `python -m compileall -q src`, and confirm package metadata/source packaging contents.
- [ ] Repeat a targeted secret/privacy search on the **exact** Git-tracked file set and check for naming remnants not intended for public release.
- [ ] Verify `LICENSE` copyright notice and `pyproject.toml` MIT metadata. Document releases accurately (Linux source verified; Windows binary only).

## Complete before attaching Windows x64 binary

- [ ] Build again from the final staged source using `scripts/build_windows_release.ps1` and the authentic embeddable Python ZIP.
- [ ] Verify no stale build outputs are included; run `scripts/check_distribution.py` against the new distribution.
- [ ] Test extracted distribution in an isolated writable location or clean Windows VM with no system Python required: first run, GUI close/reopen, offline demo, SQLite comparison **DIFFERENT → accept → SAME**, history preservation, settings persistence, and no ClickUp credentials needed.
- [ ] Inspect `dist/Automator` for private data and licenses of shipped dependencies (including MPL 2.0, Apache NOTICE, embedded CPython, PyInstaller and transitive components); provide required complete notices/source-access information.
- [ ] Produce a SHA-256 checksum for the ZIP and include it with the GitHub Release; manually verify extraction and checksum.

## Publication

- [ ] Confirm target GitHub account/repository name and visibility.
- [ ] Create a new repository without auto-initializing a README/license if pushing an existing local repository.
- [ ] Push the **clean C: source repo** and verify GitHub's online file tree and license recognition.
- [ ] Publish `v0.1.0` release notes and optionally the verified Windows x64 ZIP with its checksum and notices.
- [ ] Verify fresh-user README links, installation directions, and issue reporting instructions.
