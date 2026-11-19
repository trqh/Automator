"""Safe initialization of feature directories from bundled templates."""

from __future__ import annotations
from importlib.resources import files
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

TEMPLATE_PACKAGE = __package__.rsplit(".", 1)[0] + ".templates"
REQUIRED_FILES = ("main.py", "script.config")
TEMPLATE_FILES = {"main.py": "feature_main.py", "script.config": "script.config"}


class FeatureSetupError(RuntimeError):
    """Raised when a feature cannot be safely initialized."""


@dataclass(frozen=True)
class SetupInspection:
    feature_number: str
    state: str
    directory: Path
    missing_files: tuple[str, ...]
    message: str = ""


def _validate_feature(feature_number: str) -> str:
    value = str(feature_number)
    if not value.isascii() or not value.isdigit():
        raise FeatureSetupError("Feature number must contain ASCII digits only.")
    return value


def inspect_feature_setup(feature_number: str, script_runs_dir: Path | str) -> SetupInspection:
    feature = _validate_feature(feature_number)
    root = Path(script_runs_dir).expanduser()
    directory = root / ("feature_" + feature)
    if not directory.exists():
        return SetupInspection(feature, "NOT INITIALIZED", directory, REQUIRED_FILES)
    if not directory.is_dir():
        return SetupInspection(feature, "PARTIAL SETUP", directory, REQUIRED_FILES,
                               "The feature path exists but is not a directory.")
    missing = []
    problems = []
    for name in REQUIRED_FILES:
        path = directory / name
        if not path.exists():
            missing.append(name)
        elif not path.is_file():
            problems.append(name + " exists but is not a regular file")
    if problems:
        return SetupInspection(feature, "PARTIAL SETUP", directory, tuple(missing), "; ".join(problems))
    state = "READY" if not missing else "PARTIAL SETUP"
    return SetupInspection(feature, state, directory, tuple(missing))


def inspect_feature_setups(features: Iterable[str], script_runs_dir: Path | str) -> Dict[str, SetupInspection]:
    return {str(feature): inspect_feature_setup(feature, script_runs_dir) for feature in features}


def initialize_feature(feature_number: str, script_runs_dir: Path | str, *, fill_missing: bool = False) -> SetupInspection:
    inspection = inspect_feature_setup(feature_number, script_runs_dir)
    if inspection.state == "READY":
        return inspection
    if inspection.message:
        raise FeatureSetupError(inspection.message)
    if inspection.state == "PARTIAL SETUP" and not fill_missing:
        raise FeatureSetupError("Feature has a partial setup; confirmation is required to add missing files.")

    template_contents = {}
    for name in inspection.missing_files:
        try:
            template_contents[name] = (
                files(TEMPLATE_PACKAGE)
                .joinpath(TEMPLATE_FILES[name])
                .read_bytes()
            )
        except OSError as exc:
            raise FeatureSetupError(
                "Could not read template %s: %s" % (name, exc)
            ) from exc

    try:
        inspection.directory.mkdir(parents=True, exist_ok=True)
        for name, content in template_contents.items():
            destination = inspection.directory / name
            try:
                with destination.open("xb") as stream:
                    stream.write(content)
            except FileExistsError:
                # A concurrent creator won the race. Preserve its file.
                continue
        result = inspect_feature_setup(inspection.feature_number, Path(script_runs_dir).expanduser())
    except OSError as exc:
        raise FeatureSetupError("Could not initialize feature_%s: %s" % (inspection.feature_number, exc)) from exc
    if result.state != "READY":
        raise FeatureSetupError(result.message or "Feature initialization is incomplete.")
    return result


def initialize_features(features: Iterable[str], script_runs_dir: Path | str, *, fill_missing: bool = False):
    outcomes = []
    for feature in features:
        try:
            outcomes.append((str(feature), initialize_feature(feature, script_runs_dir, fill_missing=fill_missing), None))
        except Exception as exc:
            outcomes.append((str(feature), None, exc))
    return outcomes
