"""Immutable, verified SQLite snapshots, locally and in GitHub Releases.

An archive becomes restorable only after a separate ready marker is uploaded.
No upload uses --clobber and no previous asset is removed automatically.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import zipfile
import re
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from locking import instance_lock


class StateError(RuntimeError):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def readonly(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise StateError('Database is missing; refusing to create an empty replacement')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)
    try:
        yield conn
    finally:
        conn.close()


def describe_db(path):
    with readonly(path) as conn:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise StateError('SQLite integrity_check failed')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'lot_history', 'ml_ads', 'price_history'}.issubset(tables):
            raise StateError('Required Sniper tables missing')
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for t in sorted(tables) if not t.startswith('sqlite_')}


def compare_history(baseline, current):
    """Allow updates/migrations, but retain IDs and immutable history rows."""
    describe_db(baseline)
    describe_db(current)
    with readonly(baseline) as before, readonly(current) as after:
        old_tables = {r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, key in [('lot_history', 'ad_id'), ('ml_ads', 'ad_id'),
                           ('price_history', 'id'), ('availability_history', 'id'),
                           ('ad_revisions', 'id'), ('notification_outbox', 'event_key')]:
            if table not in old_tables:
                continue
            # Stream rather than duplicating a potentially large history in RAM.
            cols = [r[1] for r in before.execute(f'PRAGMA table_info({table})')]
            new_cols = [r[1] for r in after.execute(f'PRAGMA table_info({table})')]
            for row in before.execute(f'SELECT * FROM {table}'):
                key_value = row[cols.index(key)]
                new = after.execute(f'SELECT * FROM {table} WHERE {key}=?', (key_value,)).fetchone()
                if new is None:
                    raise StateError(f'History lost from {table}')
                if table in ('price_history', 'availability_history', 'ad_revisions'):
                    if any(row[i] != new[new_cols.index(col)] for i, col in enumerate(cols)):
                        raise StateError(f'History changed in {table}')


def make_snapshot(db, settings, output, baseline=None):
    db, settings, output = Path(db), Path(settings), Path(output)
    describe_db(db)
    output.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output).free < db.stat().st_size * 3 + 20_000_000:
        raise StateError('Insufficient free space for a verified snapshot')
    name = 'sniper-state-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:12] + '.zip'
    final = output / name
    with tempfile.TemporaryDirectory(dir=output) as temp:
        temp = Path(temp)
        copy = temp / 'sniper.db'
        with readonly(db) as source, closing(sqlite3.connect(copy)) as target:
            source.backup(target)
        counts = describe_db(copy)
        if baseline:
            compare_history(baseline, copy)
        files = {'sniper.db': {'sha256': sha256(copy), 'size': copy.stat().st_size}}
        if settings.exists():
            raw = settings.read_bytes()
            if not isinstance(json.loads(raw), dict):
                raise StateError('Settings must be a JSON object')
            (temp / 'settings.json').write_bytes(raw)
            files['settings.json'] = {'sha256': sha256(temp / 'settings.json'), 'size': len(raw)}
        manifest = dict(format=1, created_ts=int(time.time()), files=files, counts=counts)
        archive = temp / name
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr('manifest.json', json.dumps(manifest, sort_keys=True))
            for filename in files:
                bundle.write(temp / filename, filename)
        # Exercise the same restore path before publishing even a local snapshot.
        verify_snapshot(archive, temp / 'check')
        os.replace(archive, final)
    return final


def prune_local(output, keep_recent=24, keep_daily=14):
    """Retain hourly and daily versions; never touch manually named backups."""
    output = Path(output).resolve()
    if keep_recent < 3 or keep_daily < 1:
        raise StateError('Retention must keep at least 3 recent and 1 daily snapshot')
    pattern = re.compile(r'^sniper-state-\d{8}T\d{6,12}Z-[a-f0-9]{12}\.zip$')
    files = sorted((p for p in output.glob('sniper-state-*.zip')
                    if pattern.fullmatch(p.name) and not p.is_symlink() and p.resolve().parent == output), reverse=True)
    if len(files) <= keep_recent:
        return 0
    # Validate the newest three retained snapshots before deleting any old version.
    for path in files[:3]:
        with tempfile.TemporaryDirectory() as temp:
            verify_snapshot(path, temp)
    retained = set(files[:keep_recent])
    daily = {}
    for path in files:
        daily.setdefault(path.name[13:21], path)
    retained.update(list(daily.values())[:keep_daily])
    deleted = 0
    for path in files:
        if path not in retained:
            path.unlink()
            deleted += 1
    return deleted


def verify_snapshot(archive, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or set(names) - {'manifest.json', 'sniper.db', 'settings.json'}:
            raise StateError('Unexpected archive contents')
        manifest = json.loads(bundle.read('manifest.json'))
        if manifest.get('format') != 1 or 'sniper.db' not in manifest.get('files', {}):
            raise StateError('Invalid snapshot manifest')
        if set(names) != {'manifest.json', *manifest['files']}:
            raise StateError('Manifest file set mismatch')
        for name, info in manifest['files'].items():
            path = destination / name
            with bundle.open(name) as source, open(path, 'wb') as target:
                shutil.copyfileobj(source, target)
            if path.stat().st_size != info['size'] or sha256(path) != info['sha256']:
                raise StateError('Snapshot checksum mismatch')
        if describe_db(destination / 'sniper.db') != manifest['counts']:
            raise StateError('Snapshot row counts mismatch')
        if (destination / 'settings.json').exists():
            if not isinstance(json.loads((destination / 'settings.json').read_bytes()), dict):
                raise StateError('Invalid snapshot settings')
    return manifest


def install_snapshot(archive, db, settings, overwrite=False):
    with instance_lock(str(Path(db).resolve()) + '.lock'):
        return _install_snapshot_locked(archive, db, settings, overwrite)


def _install_snapshot_locked(archive, db, settings, overwrite=False):
    db, settings = Path(db), Path(settings)
    if (db.exists() or settings.exists()) and not overwrite:
        raise StateError('Destination exists; stop the bot and explicitly select overwrite')
    if Path(str(db) + '-wal').exists() or Path(str(db) + '-shm').exists():
        raise StateError('SQLite sidecars present; restore only while the database is offline')
    db.parent.mkdir(parents=True, exist_ok=True)
    marker = Path(str(db) + '.restore-in-progress')
    if marker.exists():
        raise StateError('Interrupted restore marker exists; operator recovery is required')
    with tempfile.TemporaryDirectory(dir=db.parent) as temp:
        manifest = verify_snapshot(archive, temp)
        # Validation finishes before either live file is touched.
        if overwrite and db.exists():
            try:
                make_snapshot(db, settings, db.parent / 'backups')
            except (StateError, sqlite3.Error):
                # Retain corrupt bytes for investigation while allowing recovery.
                preserved = db.parent / 'backups' / ('unverified-before-restore-' + uuid.uuid4().hex + '.db')
                preserved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(db, preserved)
        if not Path(temp, 'settings.json').exists() and settings.exists() and overwrite:
            raise StateError('Snapshot has no settings; refusing to mix it with existing settings')
        marker.write_text(json.dumps(dict(archive=str(Path(archive).resolve()))), encoding='utf-8')
        if Path(temp, 'settings.json').exists():
            settings.parent.mkdir(parents=True, exist_ok=True)
            staged = settings.with_name(settings.name + '.restore-tmp')
            shutil.copyfile(Path(temp, 'settings.json'), staged)
            os.replace(staged, settings)
        os.replace(Path(temp, 'sniper.db'), db)
        marker.unlink()
    return manifest


class GhRelease:
    def __init__(self, repo):
        self.repo = repo

    def run(self, *args):
        try:
            result = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired as exc:
            raise StateError('GitHub operation timed out: ' + args[0]) from exc
        if result.returncode:
            # Do not echo subprocess stderr, auth details or user-supplied URLs.
            status = re.search(r'HTTP\s+(\d{3})',result.stderr or '')
            raise StateError('GitHub operation failed: ' + args[0] + (' HTTP ' + status[1] if status else ''))
        return result.stdout

    def assets(self):
        try:
            release = json.loads(self.run('api', f'repos/{self.repo}/releases/tags/db-state'))
            # The embedded assets array must not truncate older snapshots/leases.
            pages = json.loads(self.run('api', '--paginate', '--slurp', f"repos/{self.repo}/releases/{release['id']}/assets?per_page=100"))
            return [asset for page in pages for asset in page]
        except StateError:
            # An API failure or hidden/private repo 404 is NEVER inferred as bootstrap.
            raise StateError('Cannot read db-state; existing state must be recovered explicitly')

    def download(self, name, directory):
        self.run('release', 'download', 'db-state', '-R', self.repo, '-p', name, '-D', str(directory))
        return Path(directory) / name

    def upload(self, path):
        self.run('release', 'upload', 'db-state', str(path), '-R', self.repo)

    def is_private(self):
        info = json.loads(self.run('api', f'repos/{self.repo}'))
        if not isinstance(info.get('private'), bool):
            raise StateError('Cannot determine state repository visibility')
        return info['private']


def begin_run(store, run_id):
    if not run_id or any(c not in '0123456789-' for c in run_id):
        raise StateError('Invalid run ID')
    names = {a['name'] for a in store.assets()}
    assert_no_unfinished_runs(names)
    if len(names) > 950:
        raise StateError('db-state is near the 1000-asset limit; archive older versions before starting another shift')
    with tempfile.TemporaryDirectory() as temp:
        marker = Path(temp) / f'sniper-run-{run_id}.start.json'
        marker.write_text(json.dumps(dict(run_id=run_id, started_ts=int(time.time()))), encoding='utf-8')
        store.upload(marker)
        with tempfile.TemporaryDirectory() as check:
            if store.download(marker.name, check).read_bytes() != marker.read_bytes():
                raise StateError('Run start verification failed')


def finish_run(store, run_id, archive_name):
    names = {a['name'] for a in store.assets()}
    if f'sniper-run-{run_id}.start.json' not in names or archive_name.removesuffix('.zip') + '.ready.json' not in names:
        raise StateError('Cannot finish run without a committed snapshot and start record')
    with tempfile.TemporaryDirectory() as temp:
        marker = Path(temp) / f'sniper-run-{run_id}.done.json'
        marker.write_text(json.dumps(dict(run_id=run_id, archive=archive_name, finished_ts=int(time.time()))), encoding='utf-8')
        store.upload(marker)
        with tempfile.TemporaryDirectory() as check:
            if store.download(marker.name, check).read_bytes() != marker.read_bytes():
                raise StateError('Run finish verification failed')


def assert_no_unfinished_runs(names):
    for name in names:
        if name.startswith('sniper-run-') and name.endswith('.start.json'):
            done = name.removesuffix('.start.json') + '.done.json'
            if done not in names:
                raise StateError('Previous shift has no confirmed save. Recover its artifact/state before another shift')


def publish(store, archive):
    """Upload -> download and verify -> publish immutable commit marker."""
    archive = Path(archive)
    store.upload(archive)
    with tempfile.TemporaryDirectory() as temp:
        downloaded = store.download(archive.name, temp)
        if sha256(downloaded) != sha256(archive):
            raise StateError('Remote snapshot checksum mismatch')
        verify_snapshot(downloaded, Path(temp) / 'verify')
        ready = Path(temp) / (archive.stem + '.ready.json')
        ready.write_text(json.dumps(dict(format=1, archive=archive.name, sha256=sha256(archive), size=archive.stat().st_size)), encoding='utf-8')
        store.upload(ready)
        with tempfile.TemporaryDirectory() as check:
            if store.download(ready.name, check).read_bytes() != ready.read_bytes():
                raise StateError('Remote ready marker verification failed')
    return archive.name


def restore_release(store, db, settings, baseline):
    assets = store.assets()
    names = {a['name'] for a in assets}
    assert_no_unfinished_runs(names)
    markers = sorted((n for n in names if n.startswith('sniper-state-') and n.endswith('.ready.json')), reverse=True)
    for marker in markers:
        try:
            with tempfile.TemporaryDirectory() as temp:
                info = json.loads(store.download(marker, temp).read_bytes())
                expected = marker.removesuffix('.ready.json') + '.zip'
                if info.get('archive') != expected or expected not in names or info.get('format') != 1:
                    raise StateError('Ready marker mismatch')
                archive = store.download(expected, temp)
                if archive.stat().st_size != info['size'] or sha256(archive) != info['sha256']:
                    raise StateError('Remote snapshot checksum mismatch')
                manifest = install_snapshot(archive, db, settings)
                Path(baseline).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(db, baseline)
                print('Restored verified snapshot:', expected, manifest['counts'])
                return expected
        except (StateError, OSError, ValueError, KeyError, zipfile.BadZipFile, sqlite3.Error):
            print('Snapshot unavailable or invalid; trying an earlier committed snapshot:', marker)
    if markers:
        raise StateError('No committed snapshot can be restored; legacy fallback requires explicit operator choice')
    # One-time migration from supplied v3 workflow, without modifying the old asset.
    if 'sniper.db' in names:
        with tempfile.TemporaryDirectory() as temp:
            source = store.download('sniper.db', temp)
            describe_db(source)
            if 'settings.json' in names:
                store.download('settings.json', temp)
            archive = make_snapshot(source, Path(temp) / 'settings.json', Path(temp) / 'new')
            install_snapshot(archive, db, settings)
            Path(baseline).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(db, baseline)
            return 'legacy-sniper.db'
    raise StateError('Release contains no committed database. Missing state is not a first run')


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('snapshot', 'restore-local', 'restore-release', 'save-release', 'check', 'begin-run', 'finish-run', 'prune-local'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--db', default=os.getenv('DB_FILE', 'sniper.db'))
        cmd.add_argument('--settings', default=os.getenv('SETTINGS_FILE', 'settings.json'))
        cmd.add_argument('--output', default='backups')
        cmd.add_argument('--baseline')
        cmd.add_argument('--repo', default=os.getenv('DB_REPO'))
        cmd.add_argument('--archive')
        cmd.add_argument('--overwrite', action='store_true')
        cmd.add_argument('--run-id', default=os.getenv('SNIPER_RUN_ID'))
        cmd.add_argument('--keep-recent', type=int, default=24)
        cmd.add_argument('--keep-daily', type=int, default=14)
    args = parser.parse_args()
    if args.command == 'prune-local':
        print('Removed old automatic local snapshots:', prune_local(args.output, args.keep_recent, args.keep_daily))
    elif args.command == 'begin-run':
        begin_run(GhRelease(args.repo), args.run_id)
    elif args.command == 'finish-run':
        finish_run(GhRelease(args.repo), args.run_id, Path(args.archive).name)
    elif args.command == 'check':
        print(describe_db(args.db))
    elif args.command == 'restore-local':
        print(install_snapshot(args.archive, args.db, args.settings, args.overwrite))
    elif args.command == 'restore-release':
        store = GhRelease(args.repo)
        private = store.is_private()
        restore_release(store, args.db, args.settings, args.baseline or 'state/restore-baseline.db')
        if os.getenv('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
                output.write('state_repo_private=' + str(private).lower() + '\n')
    else:
        archive = make_snapshot(args.db, args.settings, args.output, args.baseline)
        if args.command == 'save-release':
            publish(GhRelease(args.repo), archive)
            if args.run_id:
                finish_run(GhRelease(args.repo), args.run_id, archive.name)
        print('Verified snapshot:', archive)


if __name__ == '__main__':
    try:
        main()
    except (StateError, OSError, ValueError, KeyError, sqlite3.Error, zipfile.BadZipFile) as exc:
        raise SystemExit(f'State operation stopped: {type(exc).__name__}: {exc}')
