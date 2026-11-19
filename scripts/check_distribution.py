"""Read-only verification of Windows Automator distribution."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=45, check=False)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {command!r}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("distribution", type=Path)
    args = parser.parse_args()
    root = args.distribution.resolve()
    gui = root / "Automator.exe"
    python = root / "runtime" / "python.exe"
    assert gui.is_file(), f"Missing GUI: {gui}"
    assert python.is_file(), f"Missing bundled interpreter: {python}"
    assert (root / "runtime" / "Lib" / "site-packages" / "automator").is_dir(), "Bundled helper package absent"

    # Simulate absence of a system Python on PATH. Do not alter the real machine.
    env = dict(os.environ)
    env["PATH"] = str(python.parent) + os.pathsep + str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("AUTOMATOR_BUNDLED_PYTHON", None)
    # Verify binary behavior and the ClickUp helper module imports without network calls.
    checks = {
        "python_version": run([str(python), "-c", "import sys;print(sys.version.split()[0])"], cwd=root, env=env),
        "demo_dependencies": run([str(python), "-c", "import sqlite3;print(sqlite3.sqlite_version)"], cwd=root, env=env),
        "clickup_import": run([str(python), "-c", "import automator.integrations.clickup.sync_clickup_task_ids;print('OK')"], cwd=root, env=env),
        "api_import": run([str(python), "-c", "import requests,psutil,dateutil,dotenv;print('OK')"], cwd=root, env=env),
    }
    print(json.dumps({"distribution": str(root), "checks": checks}, indent=2))
    print("I15C bundled-runtime static verification: PASS")
    print("NEXT: manually run Automator.exe and Run Demo with an isolated data directory.")


if __name__ == "__main__":
    main()
