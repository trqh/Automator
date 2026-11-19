from pathlib import Path

from src.automator.infrastructure.canonical_paths import canonical_paths


def test_portable_storage_root(tmp_path):
    paths = canonical_paths(tmp_path / 'app-data', tmp_path / 'app-data' / 'features')
    assert paths.outputs == (tmp_path / 'app-data' / 'Canonical Outputs').resolve()
    assert paths.history == (tmp_path / 'app-data' / 'Canonical Output History').resolve()


def test_development_storage_is_unchanged(tmp_path):
    app = tmp_path / 'Script Runs' / 'Automator'
    paths = canonical_paths(app, app.parent)
    assert paths.outputs == (app.parent / 'Canonical Outputs').resolve()
    assert paths.history == (app.parent / 'Canonical Output History').resolve()




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
