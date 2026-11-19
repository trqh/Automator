# Security policy

## Reporting a vulnerability

Please avoid posting credentials, live access tokens, personally identifying records, or exploit details in public issues. If the repository owner has GitHub private vulnerability reporting enabled, use **Security → Report a vulnerability** in the repository. Otherwise, open a public issue with only a non-sensitive summary and request a private reporting channel.

## Supported versions

The initial version is `0.1.x`. Security fixes, if available, will target the current published release; no fixed response or support period is guaranteed.

## Security boundaries

- Python feature scripts execute code with your user's privileges. **Only run scripts you trust.** Automator is not a sandbox.
- ClickUp integration uses credentials you provide; do not commit tokens or include them in support tickets.
- Review SQLite outputs and canonical acceptance carefully; do not use real/private databases in public bug reports.
- Download binaries only from the project's official GitHub Releases and verify checksums where provided.
- This application is provided under the MIT License, without warranty.
