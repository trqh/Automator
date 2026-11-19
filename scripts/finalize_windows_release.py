#!/usr/bin/env python3
"""Guarded post-build cleaner and ZIP packager for Automator releases.

Run on Windows after build_windows_release.ps1. No changes outside dist/Automator.
The separate source tree and user-data directories are never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

BAD_RUNTIME_RELATIVE = (
    '_internal/automator/ui/data/gui_state.json',
    '_internal/automator/ui/logs/gui_actions.jsonl',
    '_internal/automator/ui/logs/structured_events.jsonl',
    '_internal/automator/ui/logs/work_tracking.sqlite3',
)
DISALLOWED_NAMES = {'gui_state.json', 'gui_actions.jsonl', 'structured_events.jsonl',
                    'work_tracking.sqlite3', '.env', 'records.json', 'records_meta.json'}
BAD_EXTENSIONS = {'.sqlite', '.sqlite3', '.key', '.p12', '.pfx'}

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def verify_identity(repo: Path, dist: Path) -> None:
    if not (repo / 'pyproject.toml').is_file() or not (repo / 'src' / 'automator').is_dir():
        raise ValueError('Not an Automator source staging directory')
    if dist.resolve() != (repo / 'dist' / 'Automator').resolve():
        raise ValueError('Distribution must be <stage>/dist/Automator')
    if not (dist / 'Automator.exe').is_file() or not (dist / 'runtime' / 'python.exe').is_file():
        raise ValueError('Missing Windows GUI or embedded Python runtime')
    if not (repo / 'LICENSE').is_file() or not (repo / 'THIRD_PARTY_NOTICES.md').is_file():
        raise ValueError('Missing public license or notices')

def get_licenses(site: Path):
    for info in sorted(site.glob('*.dist-info')):
        if not info.is_dir():
            continue
        for p in sorted(info.rglob('*')):
            if p.is_file() and (p.name.lower().startswith(('license','copying','notice','authors'))):
                yield info.name, p

def check_obsolete_package(repo: Path, dist: Path) -> list[str]:
    bad = []
    source = repo / 'src' / 'automator' / 'integrations' / 'dataops'
    if source.exists():
        bad.append('source: src/automator/integrations/dataops')
    for wheel in sorted((repo / 'dist').glob('automator-*.whl')):
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if name.lower().startswith('automator/integrations/dataops/'):
                    bad.append(f'wheel: {wheel.name}: {name}')
    for base in (dist / '_internal' / 'automator' / 'integrations' / 'dataops',
                 dist / 'runtime' / 'Lib' / 'site-packages' / 'automator' / 'integrations' / 'dataops'):
        if base.exists():
            bad.append('distribution: ' + base.relative_to(dist).as_posix())
    return bad

def check_for_data(dist: Path) -> list[str]:
    offenders = []
    for p in dist.rglob('*'):
        if not p.is_file():
            continue
        rel = p.relative_to(dist).as_posix()
        # Only flag our package's generated state, not legitimate vendor test files.
        if rel.startswith('_internal/automator/') or rel.startswith('runtime/Lib/site-packages/automator/'):
            if p.name.lower() in DISALLOWED_NAMES or p.suffix.lower() in BAD_EXTENSIONS or '/logs/' in rel or '/data/' in rel:
                offenders.append(rel)
    return offenders

def check_local_metadata(dist: Path) -> list[str]:
    return [p.relative_to(dist).as_posix() for p in dist.rglob('direct_url.json')
            if p.is_file() and re.fullmatch(r'automator-[^/\\]+\.dist-info', p.parent.name, flags=re.IGNORECASE)]

def find_licenses(repo: Path, dist: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    items = [('Automator/LICENSE', repo / 'LICENSE'),
             ('Automator/THIRD_PARTY_NOTICES.md', repo / 'THIRD_PARTY_NOTICES.md'),
             ('CPython/LICENSE.txt', dist / 'runtime' / 'LICENSE.txt')]
    missing = []
    site = dist / 'runtime' / 'Lib' / 'site-packages'
    infos = sorted(x for x in site.glob('*.dist-info') if x.is_dir())
    for info in infos:
        found = False
        for package, path in get_licenses(site):
            if package != info.name:
                continue
            found = True
            items.append(('Python_packages/' + info.name + '/' + path.relative_to(info).as_posix(), path))
        if not found:
            missing.append(info.name)
    tk = dist / '_internal' / '_tk_data' / 'license.terms'
    if tk.is_file(): items.append(('Tcl_Tk/license.terms', tk))
    else: missing.append('Tk license.terms (_internal/_tk_data)')
    # PyInstaller runtime contains a bootloader; preserve its package license separately.
    build_site = repo / '.venv-release' / 'Lib' / 'site-packages'
    pi = sorted(build_site.glob('pyinstaller-*.dist-info')) + sorted(build_site.glob('PyInstaller-*.dist-info'))
    pi = list(dict.fromkeys(pi))
    if not pi:
        missing.append('PyInstaller distribution metadata (build venv)')
    else:
        pi_files = [(x,p) for x,p in get_licenses(build_site) if 'pyinstaller-' in x.lower()]
        if not pi_files:
            missing.append('PyInstaller license text (build venv)')
        for x,p in pi_files:
            items.append(('PyInstaller/'+x+'/'+p.relative_to(build_site/x).as_posix(),p))
    # Additional Python install notices, if any.
    return items, missing

def write_report(report: Path, data: dict) -> None:
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')

def main() -> int:
    pa = argparse.ArgumentParser(description=__doc__)
    pa.add_argument('--stage', required=True, type=Path)
    pa.add_argument('--mode', choices=['audit','prepare','zip'], default='audit')
    pa.add_argument('--zip-out', type=Path)
    pa.add_argument('--report', type=Path)
    args = pa.parse_args()
    repo = args.stage.resolve()
    dist = repo / 'dist' / 'Automator'
    verify_identity(repo, dist)
    obsolete = check_obsolete_package(repo, dist)
    if obsolete:
        raise RuntimeError('Obsolete dataops package detected: ' + '; '.join(obsolete))
    bad_before = check_for_data(dist)
    local_metadata = check_local_metadata(dist)
    licenses, missing = find_licenses(repo,dist)
    changes = []
    if args.mode != 'audit':
        # Remove ONLY the four known generated files; fail on any new/unrecognized data.
        unknown = sorted(set(bad_before).difference(BAD_RUNTIME_RELATIVE))
        if unknown:
            raise RuntimeError('Unrecognized application data in build: ' + ', '.join(unknown))
        for rel in BAD_RUNTIME_RELATIVE:
            p = dist / rel
            if p.is_file():
                p.unlink()
                changes.append('removed '+rel)
        for rel in local_metadata:
            p = dist / rel
            p.unlink()
            changes.append('removed local install metadata '+rel)
        if check_obsolete_package(repo, dist):
            raise RuntimeError('Obsolete dataops package detected after cleanup')
        if check_for_data(dist) or check_local_metadata(dist):
            raise RuntimeError('Data or local metadata survived cleaning')
        notice_root = dist / 'THIRD_PARTY_LICENSES'
        if notice_root.exists():
            # Avoid carrying stale notices from a previous build.
            shutil.rmtree(notice_root)
        for name, src in licenses:
            dest = notice_root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        changes.append('assembled '+str(len(licenses))+' notice files')
        if missing:
            raise RuntimeError('Missing upstream license texts: '+', '.join(missing)+'. Review before release.')
        # Reports should live outside dist (and not be included in the published ZIP).
    issues = check_for_data(dist) if args.mode != 'audit' else bad_before
    report = {
      'mode': args.mode,
      'stage': str(repo),
      'distribution': str(dist),
      'generated_app_data_found_before': bad_before,
      'remaining_generated_app_data': issues,
      'local_install_metadata_before': local_metadata,
      'missing_license_sources': missing,
      'license_files_collected': len(licenses),
      'changes': changes,
      'zip': None,
      'zip_sha256': None,
      'release_approved': False,
      'note': 'Mechanical checks only; final review of third-party license obligations, binary contents, and clean-install behavior is still required.'
    }
    if args.mode == 'zip':
        if args.zip_out is None:
            raise ValueError('--zip-out is required for --mode zip')
        if issues or missing:
            raise RuntimeError('Cannot create ZIP while data or license issues remain')
        out = args.zip_out.resolve()
        if out == dist or dist in out.parents or repo in out.parents:
            raise ValueError('ZIP must be outside the public staging project')
        if out.exists():
            raise FileExistsError(f'Refusing to overwrite existing release ZIP: {out}')
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for p in sorted(x for x in dist.rglob('*') if x.is_file()):
                z.write(p,arcname='Automator/'+p.relative_to(dist).as_posix())
        with zipfile.ZipFile(out) as z:
            if z.testzip() is not None:
                raise RuntimeError('ZIP CRC validation failed')
        report['zip'] = str(out)
        report['zip_sha256'] = sha256(out)
        print('ZIP CREATED:',out)
        print('SHA256:',report['zip_sha256'])
    path = args.report.resolve() if args.report else repo.parent / 'AUTOMATOR_RELEASE_FINALIZATION.json'
    write_report(path,report)
    print('RELEASE FINALIZER',args.mode.upper(),'PASS' if not issues and not missing else 'REVIEW REQUIRED')
    print('Report:',path)
    print('Generated data found:',len(bad_before),'remaining:',len(issues))
    print('Upstream licenses:',len(licenses),'missing sources:',len(missing))
    if missing:print('Missing:', '; '.join(missing))
    return 0 if not issues and not missing else 2

if __name__ == '__main__':
    try:sys.exit(main())
    except Exception as exc:
        print('FINALIZER ERROR:',exc,file=sys.stderr)
        sys.exit(1)
