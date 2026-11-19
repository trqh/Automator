from src.automator.infrastructure.runtime_config import RuntimeSettings
from src.automator.services.first_run import initialize_first_run
from src.automator.infrastructure.canonical_paths import canonical_paths


def test_fresh_install_creates_both_canonical_directories(tmp_path):
    root = tmp_path / 'app_root'
    initialize_first_run(RuntimeSettings(data_root=root))
    dirs = canonical_paths(root, root / 'features')
    assert dirs.outputs.is_dir()
    assert dirs.history.is_dir()


def test_existing_canonicals_survive_repeat_first_run(tmp_path):
    root = tmp_path / 'app_root'
    settings = RuntimeSettings(data_root=root)
    initialize_first_run(settings)
    paths = canonical_paths(root, root / 'features')
    old = paths.outputs / '2695.sqlite'
    old.write_bytes(b'original canonical bytes')
    initialize_first_run(settings)
    assert old.read_bytes() == b'original canonical bytes'
