"""I15A packaging metadata and installed resource regression checks."""
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from importlib.resources import files

ROOT = Path(__file__).resolve().parents[1]


def test_package_metadata_and_console_entry_point():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = meta["project"]
    assert project["requires-python"] == ">=3.10"
    assert project["scripts"]["automator"] == "automator.ui.feature_manager_gui:main"
    names = {dep.split(">=")[0] for dep in project["dependencies"]}
    assert {"psutil", "python-dateutil", "requests", "python-dotenv", "webdriver-manager"} <= names


def test_packaged_resources_are_declared_and_present():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    resources = meta["tool"]["setuptools"]["package-data"]
    assert {"feature_main.py", "script.config"} <= set(resources["automator.templates"])
    assert {"main.py", "script.config"} <= set(resources["automator.demo_scripts.feature_900001"])
    for package, names in (
        ("src.automator.templates", ("feature_main.py", "script.config")),
        ("src.automator.demo_scripts.feature_900001", ("main.py", "script.config")),
    ):
        for name in names:
            assert files(package).joinpath(name).is_file(), f"Missing resource: {package}/{name}"


def test_importable_packaged_gui_entry_point():
    from src.automator.ui.feature_manager_gui import main
    assert callable(main)
