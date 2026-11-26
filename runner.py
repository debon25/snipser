"""Single-instance runner. Configuration comes from the environment only."""
import argparse
import atexit
import os
from pathlib import Path
import sys
import time
from locking import instance_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--initialize', action='store_true', help='Explicitly create a NEW empty database; never use to recover lost state')
    parser.add_argument('--init-only', action='store_true')
    args = parser.parse_args()
    db = Path(os.getenv('DB_FILE', 'sniper.db')).resolve()
    settings = Path(os.getenv('SETTINGS_FILE', str(db.parent / 'settings.json'))).resolve()
    os.environ['DB_FILE'] = str(db)
    os.environ['SETTINGS_FILE'] = str(settings)
    if Path(str(db) + '.restore-in-progress').exists():
        raise SystemExit('Interrupted restore detected. Finish recovery before starting the bot.')
    if not db.exists() and not args.initialize:
        raise SystemExit('Database missing. Restore a verified snapshot; creation requires --initialize explicitly.')
    if args.initialize and db.exists():
        raise SystemExit('Database already exists; --initialize refused.')
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = instance_lock(str(db) + '.lock')
        atexit.register(lock.close)
    except OSError:
        raise SystemExit('Another Sniper instance is already using this database.')
    if args.initialize:
        os.environ['SNIPER_ALLOW_NEW_DB'] = '1'
    else:
        from state_store import describe_db, make_snapshot, readonly
        describe_db(db)
        # Preserve the exact pre-migration schema and data before importing sniper.
        with readonly(db) as source:
            migrated = source.execute("SELECT 1 FROM meta WHERE key='migration_v40_done'").fetchone()
        archive = make_snapshot(db, settings, db.parent / 'backups')
        if not migrated:
            archive.rename(archive.with_name('pre-v40-' + archive.name))
    if not args.init_only and (not os.getenv('TG_TOKEN') or not os.getenv('TG_CHAT_ID')):
        raise SystemExit('TG_TOKEN and TG_CHAT_ID must be supplied via the environment.')
    import sniper
    if args.init_only:
        sniper.close_db()
        print('Local database initialized/checked; no network calls made.')
        return
    try:
        from reliability import watchdog
        watchdog(ready=True)
        sniper.main()
    finally:
        sniper.close_db()
        # A final local snapshot is useful even if a network failure stops a shift.
        from state_store import make_snapshot
        make_snapshot(db, settings, db.parent / 'backups')


if __name__ == '__main__':
    main()
