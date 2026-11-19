# Third-party software and notices

Automator's original source is licensed under the MIT License (see `LICENSE`).
Third-party packages and runtimes retain their own copyrights and licenses.

This table reflects the **Windows source/test environment inventory collected on
2026-11-18**, not a definitive list of files included in a standalone build.
The release process must audit the **actual distributed artifact** and bundle
applicable complete license texts and required notices alongside it.

| Package | Observed version | License reported by installed metadata |
|---|---|---|
| certifi | 2026.10.23 | MPL-2.0 |
| charset-normalizer | 3.5.2 | MIT |
| colorama | 0.4.6 | BSD (classifier; verify exact license text) |
| idna | 3.20 | BSD-3-Clause |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| psutil | 7.2.2 | BSD-3-Clause |
| python-dateutil | 4.0.0 | BSD-3-Clause |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| requests | 2.34.2 | Apache-2.0 (includes upstream NOTICE) |
| tzdata | 2026.6 | Apache-2.0 |
| urllib3 | 2.8.0 | MIT |
| webdriver-manager | 4.1.2 | Apache-2.0 |

Development-only tooling seen in the audit: pytest, iniconfig, pluggy,
Pygments, pip. Whether these belong in a release notice depends on inclusion
in the **actual shipped payload**; do not mistake the test environment's
inventory for a complete release bill of materials.

## Before distribution

- Inspect the final Windows portable ZIP for the precise packages and versions
  it contains, including transitive components, the embedded CPython runtime,
  PyInstaller components and any bundled binary libraries.
- Distribute full upstream license texts and required copyright/NOTICE files
  for software shipped in the artifact, not just this summary table.
- For any MPL-2.0-covered binaries/files actually distributed (such as
  `certifi`), satisfy its notice and covered-source availability requirements.
- Confirm the embedded Python/CPython license and any included third-party
  runtime notices are present.
- Re-generate this summary whenever package versions or the build change.

This file is an audit checklist and summary; it does **not** itself discharge
all third-party redistribution obligations.
