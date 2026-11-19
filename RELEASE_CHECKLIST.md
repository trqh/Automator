# Automator v0.1.1 — Release acceptance checklist

**Release gate.** This checklist reflects reported Windows verification, not a declaration that v0.1.1 has been published. Keep the published v0.1.0 artifacts unchanged.

## Verified on the Windows development host

- [x] 208 regression tests passed, zero skipped, following H6 first-run ordering correction (user-reported Windows result).
- [x] Feature 900003 registers automatically in new isolated user data, with `READY` setup state.
- [x] Bundled Windows Python executes Feature 900003 without a selected external interpreter.
- [x] Frozen GUI acceptance: 12/12 checks passed, including no canonical, approval, same/different, `DEMO-004`, history, and relaunch persistence.
- [x] Python 3.9.7 interpreter selection reported a 0.123-second probe and responsive GUI; the original timeout cause was not definitively reproduced.
- [x] v0.1.1 Windows builder reported PASS; static bundled-runtime verification passed.
- [x] Finalizer reported zero generated user-data files remaining and 22 upstream license sources found, zero missing.
- [x] Automated release-candidate audit reported no flagged sensitive paths or secret patterns; independent Gitleaks reports for Git history, `src`, `tests`, and `scripts` contained no findings.
- [x] Obsolete Feature 900001/demo modules and GUI controls were removed from the active staging source.
- [x] Simulated fallback security test verified the rejection policy when ordinary Windows sessions initially denied link creation.
- [x] Real Windows symlink rejection verified separately: the test script confirmed `Symlink: True` and `PASS: real Windows symbolic link rejected by bundled-runtime allowlist`.

## Still required before public release

- [ ] Review updated README, INSTALLATION, CHANGELOG, and this checklist against final staged code.
- [ ] Review the exact staged Git diff and **newly added untracked files**, check whitespace, and verify no accidental files or secrets enter the commit.
- [ ] Confirm security scan exit statuses and inspect any unusual findings; empty JSON reports do not replace a manual review.
- [ ] Confirm shipping third-party license texts and notices meet obligations for all components actually distributed, including embedded CPython, PyInstaller, transitive dependencies, and any applicable MPL source access.
- [ ] Run final clean staging tests with explicit isolated pytest `--basetemp` and compile checks after all patches.
- [ ] Validate final wheel metadata and Feature 900003 package resources; confirm final executable and bundled runtime remain consistent with source.
- [ ] Produce final release ZIP, record SHA-256, and verify the extracted archive and checksum in an isolated directory.
- [ ] Review full Git staged inventory/diff; commit only intended files and tag v0.1.1 only after approval.
- [ ] Push final reviewed commit/tag to the correct GitHub repository and publish release assets only after acceptance; verify online asset checksum, release notes, and download links.

## Platform scope

- Windows 11 x64 was tested with isolated application data in the development host's frozen executable; this is **not** an independent clean Windows VM claim.
- Ubuntu 26.04.1 WSL2 GUI and 195-test suite were verified for v0.1.0; **v0.1.1 Linux acceptance not verified**.
- Other Linux distributions and macOS have not completed v0.1.1 acceptance testing.

## Publication

Do not replace or delete the v0.1.0 GitHub release. Publish v0.1.1 as a separate release only when the remaining gates are satisfied.
