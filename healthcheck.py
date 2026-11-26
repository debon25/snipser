"""Exit nonzero for stalled feed or main loop. Does not send messages."""
import argparse
import os
from pathlib import Path
import time
import shutil
from state_store import readonly


def check(db, now=None, max_feed_age=900, max_loop_age=300):
    now = int(now or time.time())
    with readonly(db) as conn:
        meta = dict(conn.execute("SELECT key,value FROM meta WHERE key IN ('heartbeat_ts','last_feed_ok_ts')"))
        pending = conn.execute("SELECT COUNT(*) FROM notification_outbox WHERE state IN ('pending','sending')").fetchone()[0]
        oldest = conn.execute("SELECT MIN(created_ts) FROM notification_outbox WHERE state IN ('pending','sending')").fetchone()[0]
    errors = []
    for key, limit in [('heartbeat_ts', max_loop_age), ('last_feed_ok_ts', max_feed_age)]:
        if now - int(meta.get(key) or 0) > limit:
            errors.append(key + ': stale')
    if oldest and now - oldest > 1800:
        errors.append('notification backlog older than 30 minutes')
    if shutil.disk_usage(Path(db).resolve().parent).free < max(256_000_000,Path(db).stat().st_size*4):
        errors.append('low free disk space')
    return errors, pending


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=os.getenv('DB_FILE', 'sniper.db'))
    args = parser.parse_args()
    errors, pending = check(args.db)
    print('pending:', pending, '; '.join(errors) if errors else 'healthy')
    raise SystemExit(bool(errors))
