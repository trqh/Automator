"""Non-destructive installation of the packaged fictional-person example."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

EXAMPLE_NUMBER = "900003"
EXAMPLE_PACKAGE = "automator.demo_scripts.feature_900003"
EXAMPLE_RECORD = {
    "feature_number": EXAMPLE_NUMBER,
    "status": "To be analyzed",
    "frequency": "",
    "last_update": "",
    "clickup_task_id": "",
    "clickup_status": "",
    "notes": "Offline fictional-person SQLite example (no ClickUp required).",
    "bugs": "none",
}


def install_first_run_example(workspace: Path) -> Path | None:
    """Install the example only when its entire feature folder is absent.

    Return the installed directory, or None on collision. An existing folder,
    whether READY or partial, remains completely untouched.
    """
    destination = workspace / f"feature_{EXAMPLE_NUMBER}"
    try:
        destination.mkdir(exist_ok=False)
    except FileExistsError:
        return None

    written: list[Path] = []
    try:
        source = files(EXAMPLE_PACKAGE)
        for name in ("main.py", "script.config"):
            content = source.joinpath(name).read_bytes()
            target = destination / name
            with target.open("xb") as output:
                output.write(content)
                output.flush()
            written.append(target)
    except BaseException:
        # Only remove files we created in this call; never touch preexisting data.
        for target in reversed(written):
            target.unlink(missing_ok=True)
        try:
            destination.rmdir()
        except OSError:
            pass
        raise
    return destination
