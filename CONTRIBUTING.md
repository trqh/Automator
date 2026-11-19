# Contributing

Thanks for helping improve Automator! Contributions, tests, and issue reports are welcome.

## Development setup

Use Python 3.10+ and create a virtual environment. Install in editable mode and add pytest:

```sh
python -m pip install -e . pytest
python -m pytest -q tests
```

Windows build/installation and Ubuntu/Linux installation instructions are in [INSTALLATION.md](INSTALLATION.md).

## Proposing changes

- Open an issue describing the problem or enhancement, ideally with a minimal **synthetic** reproduction.
- Keep changes focused and add tests for behavior changes.
- Preserve compatibility with existing stored records and canonical SQLite history.
- Do not check in tokens, user records, logs, `.env` files, proprietary material, or actual ClickUp identifiers.
- Use a pull request that summarizes scope, risks, and test results. Maintainers may request changes before merging.

## Future workflow configuration

Custom statuses and ClickUp mappings are planned rather than part of the initial release. Changes should maintain backward-compatible defaults and handle removed/renamed statuses safely.

By submitting a contribution, you agree that your submitted contribution may be distributed under this repository's MIT License. Only submit code you are authorized to license.
