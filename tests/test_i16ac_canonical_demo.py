from pathlib import Path

from src.automator.infrastructure.canonical_paths import canonical_paths
from src.automator.services.comparison_demo import prepare_comparison_demo, accept_comparison_demo


def test_portable_storage_root(tmp_path):
    paths = canonical_paths(tmp_path / 'app-data', tmp_path / 'app-data' / 'features')
    assert paths.outputs == (tmp_path / 'app-data' / 'Canonical Outputs').resolve()
    assert paths.history == (tmp_path / 'app-data' / 'Canonical Output History').resolve()


def test_development_storage_is_unchanged(tmp_path):
    app = tmp_path / 'Script Runs' / 'Automator'
    paths = canonical_paths(app, app.parent)
    assert paths.outputs == (app.parent / 'Canonical Outputs').resolve()
    assert paths.history == (app.parent / 'Canonical Output History').resolve()


def test_comparison_demo_requires_acceptance(tmp_path):
    demo = prepare_comparison_demo(tmp_path)
    assert demo.initial_result.state == 'DIFFERENT'
    assert demo.candidate.is_file() and demo.canonical.is_file()
    assert not (demo.run_directory / 'Canonical Output History').exists()
    result = accept_comparison_demo(demo)
    assert result.state == 'SAME'
    history_files = list((demo.run_directory / 'Canonical Output History' / '900002').glob('*.sqlite'))
    assert len(history_files) == 1


def test_second_demo_isolated(tmp_path):
    first = prepare_comparison_demo(tmp_path)
    second = prepare_comparison_demo(tmp_path)
    assert first.run_directory != second.run_directory
    assert first.canonical != second.canonical


def test_same_day_approvals_preserve_every_version(tmp_path):
    from src.automator.workers.sqlite_compare_worker import safe_replace_from_candidate
    canonical = tmp_path / 'Canonical Outputs' / '881.sqlite'
    candidate = tmp_path / 'candidate.sqlite'
    canonical.parent.mkdir()
    canonical.write_bytes(b'old')
    candidate.write_bytes(b'middle')
    safe_replace_from_candidate(candidate, canonical)
    candidate.write_bytes(b'new')
    safe_replace_from_candidate(candidate, canonical)
    snapshots = sorted((tmp_path / 'Canonical Output History' / '881').glob('*.sqlite'))
    assert len(snapshots) == 2
    assert {p.read_bytes() for p in snapshots} == {b'old', b'middle'}
    assert canonical.read_bytes() == b'new'
