"""Durable inbox/outbox and additive data migration. No network calls on import."""
import hashlib
import json
import sqlite3
import time
import os
import socket
from datetime import datetime, timezone


class RetryAd(Exception):
    """A temporary failure; retain the ad in the inbox."""


def event_key(ad_id, price):
    # One notification per ad/asking price, including across restarts.
    return f"{ad_id}:{round(price * 100)}"


def migrate(conn):
    with conn:
        for table in ("lot_history", "ml_ads"):
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in (
                ("legacy_status", "TEXT"), ("legacy_estimated_sold_price", "REAL"),
                ("last_active_ts", "INTEGER"), ("unavailable_detected_ts", "INTEGER"),
                ("unavailable_reason", "TEXT"), ("confirmed_sale_price", "REAL"),
                ("confirmed_sale_ts", "INTEGER"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS feed_inbox (
                ad_id TEXT PRIMARY KEY, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_ts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                observed_ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_outbox (
                event_key TEXT PRIMARY KEY, ad_id TEXT NOT NULL, price REAL NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_ts INTEGER NOT NULL DEFAULT 0,
                created_ts INTEGER NOT NULL, sent_ts INTEGER, message_id INTEGER,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_inbox_due ON feed_inbox(state, next_attempt_ts);
            CREATE INDEX IF NOT EXISTS idx_outbox_due ON notification_outbox(state, next_attempt_ts);
            CREATE TABLE IF NOT EXISTS availability_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ad_id TEXT NOT NULL,
                observed_ts INTEGER NOT NULL, last_active_ts INTEGER,
                observation TEXT NOT NULL, evidence TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ad_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ad_id TEXT NOT NULL,
                source TEXT NOT NULL, fingerprint TEXT NOT NULL,
                observed_ts INTEGER NOT NULL, payload TEXT NOT NULL,
                UNIQUE(ad_id,source,fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_revisions_ad_time ON ad_revisions(ad_id,observed_ts);
        """)
        if conn.execute("SELECT 1 FROM meta WHERE key='migration_v40_done'").fetchone():
            return
        for table in ("lot_history", "ml_ads"):
            conn.execute(f"""UPDATE {table} SET legacy_status=status,
                legacy_estimated_sold_price=estimated_sold_price, status='unavailable',
                unavailable_detected_ts=last_seen_ts, unavailable_reason='legacy_inference',
                estimated_sold_price=NULL
                WHERE status IN ('sold', 'sold_fast', 'removed_mistake',
                    'removed_anomaly', 'removed_overpriced', 'ignored_gone')""")
            conn.execute(f"UPDATE {table} SET last_active_ts=last_seen_ts WHERE status='active'")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(ml_ads)")}
        if "legacy_sold_ts" not in columns:
            conn.execute("ALTER TABLE ml_ads ADD COLUMN legacy_sold_ts INTEGER")
        conn.execute("UPDATE ml_ads SET legacy_sold_ts=sold_ts, sold_ts=NULL")
        # Recover old active ads whose evaluation/send was lost before this fix,
        # even if they have already fallen out of the latest feed pages.
        for row in conn.execute("""SELECT m.*,l.title AS lot_title FROM ml_ads m JOIN lot_history l ON l.ad_id=m.ad_id
            WHERE l.status='active' AND COALESCE(l.notified,0)=0""").fetchall():
            def old_json(value):
                try:
                    return json.loads(value or '[]')
                except (ValueError, TypeError):
                    return []
            listing = datetime.fromtimestamp(row['list_time_ts'],timezone.utc).isoformat() if row['list_time_ts'] else ''
            ad = dict(ad_id=row['ad_id'],account_id=row['account_id'],subject=row['subject'] or row['lot_title'] or '',
                      price_byn=str(round((row['price_byn'] or 0)*100)),list_time=listing,
                      ad_parameters=old_json(row['ad_parameters_json']),images=[],ad_link=row['ad_link'])
            payload = json.dumps(ad,sort_keys=True,ensure_ascii=False)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            conn.execute("""INSERT OR IGNORE INTO feed_inbox(ad_id,payload,fingerprint,observed_ts)
                VALUES (?,?,?,?)""",(row['ad_id'],payload,digest,int(time.time())))
            conn.execute("""INSERT OR IGNORE INTO ad_revisions(ad_id,source,fingerprint,observed_ts,payload)
                VALUES (?,'legacy_reconstructed',?,?,?)""",(row['ad_id'],digest,int(time.time()),payload))
        # Historical acknowledged deliveries remain suppressed at the same price.
        for row in conn.execute("""SELECT ad_id,last_seen_price FROM lot_history WHERE notified=1
            AND (status='active' OR legacy_status IN ('sold','sold_fast','removed_mistake','removed_anomaly','removed_overpriced'))"""):
            ad_id, price = row[0], row[1] or 0
            conn.execute("""INSERT OR IGNORE INTO notification_outbox
                (event_key,ad_id,price,payload,state,created_ts,last_error)
                VALUES (?,?,?,'{}','sent',?,'legacy_notified_flag;delivery_time_unknown')""",
                (event_key(ad_id, price), ad_id, price, int(time.time())))
        # Do not repeat the old heuristic migration on a new database.
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('migration_v37_done','superseded')")
        conn.execute("INSERT INTO meta VALUES ('migration_v40_done',?)", (str(int(time.time())),))


def remember_ads(conn, ads, now=None):
    """Persist raw ads before acknowledging a pagination cursor."""
    now = int(now or time.time())
    for ad in ads:
        ad_id = str(ad.get("ad_id") or "")
        if not ad_id:
            continue
        payload = json.dumps(ad, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        conn.execute("""INSERT OR IGNORE INTO ad_revisions
            (ad_id,source,fingerprint,observed_ts,payload) VALUES (?,'feed',?,?,?)""", (ad_id, digest, now, payload))
        was_unavailable = conn.execute("SELECT 1 FROM lot_history WHERE ad_id=? AND status='unavailable'", (ad_id,)).fetchone()
        conn.execute("""INSERT INTO feed_inbox(ad_id,payload,fingerprint,observed_ts)
            VALUES (?,?,?,?) ON CONFLICT(ad_id) DO UPDATE SET
            payload=excluded.payload, observed_ts=excluded.observed_ts,
            state=CASE WHEN feed_inbox.fingerprint!=excluded.fingerprint THEN 'pending' ELSE feed_inbox.state END,
            next_attempt_ts=CASE WHEN feed_inbox.fingerprint!=excluded.fingerprint THEN 0 ELSE feed_inbox.next_attempt_ts END,
            attempts=CASE WHEN feed_inbox.fingerprint!=excluded.fingerprint THEN 0 ELSE feed_inbox.attempts END,
            fingerprint=excluded.fingerprint""", (ad_id, payload, digest, now))
        if was_unavailable:
            conn.execute("UPDATE feed_inbox SET state='pending',next_attempt_ts=0 WHERE ad_id=?", (ad_id,))
        for table in ('lot_history', 'ml_ads'):
            conn.execute(f"UPDATE {table} SET last_seen_ts=?,last_active_ts=? WHERE ad_id=?", (now, now, ad_id))


def pending_ads(conn, limit=100, now=None):
    return [json.loads(r[0]) for r in conn.execute("""SELECT payload FROM feed_inbox
        WHERE state='pending' AND next_attempt_ts<=? ORDER BY attempts,observed_ts DESC,ad_id DESC LIMIT ?""",
        (int(now or time.time()), limit))]


def remember_detail(conn, ad_id, details, now=None):
    payload = json.dumps(details, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    conn.execute("""INSERT OR IGNORE INTO ad_revisions
        (ad_id,source,fingerprint,observed_ts,payload) VALUES (?,'detail',?,?,?)""",
        (ad_id, digest, int(now or time.time()), payload))


def finish_ad(conn, ad_id, error=None, now=None):
    now = int(now or time.time())
    with conn:
        if error:
            conn.execute("""UPDATE feed_inbox SET attempts=attempts+1,last_error=?,
                next_attempt_ts=?+MIN(1800,30*(1<<MIN(attempts,6))) WHERE ad_id=?""",
                (error, now, ad_id))
        else:
            conn.execute("UPDATE feed_inbox SET state='done',last_error=NULL WHERE ad_id=?", (ad_id,))


def queue_notification(conn, ad_id, price, text, photo_url=None, with_sound=False):
    key = event_key(ad_id, price)
    payload = json.dumps(dict(text=text, photo_url=photo_url, with_sound=with_sound,
                              ad_id=ad_id, asking_price=price), ensure_ascii=False)
    # A later price supersedes an unsent older price, but never deletes its record.
    conn.execute("""UPDATE notification_outbox SET state='cancelled',last_error='price_changed'
        WHERE ad_id=? AND event_key!=? AND state='pending'""", (ad_id, key))
    conn.execute("""INSERT OR IGNORE INTO notification_outbox
        (event_key,ad_id,price,payload,created_ts) VALUES (?,?,?,?,?)""",
        (key, ad_id, price, payload, int(time.time())))
    conn.execute("""UPDATE notification_outbox SET state='pending',payload=?,next_attempt_ts=0,
        last_error=NULL WHERE event_key=? AND state='cancelled'""", (payload, key))


def drain_outbox(conn, sender, limit=5, now=None):
    """At-least-once delivery. A lost HTTP acknowledgement can still duplicate a message."""
    now = int(now or time.time())
    rows = conn.execute("""SELECT * FROM notification_outbox
        WHERE state='pending' AND next_attempt_ts<=? ORDER BY created_ts,event_key LIMIT ?""", (now, limit)).fetchall()
    for row in rows:
        # Commit 'sending' first: after a crash this explicitly records uncertainty.
        with conn:
            conn.execute("UPDATE notification_outbox SET state='sending' WHERE event_key=?", (row['event_key'],))
        try:
            result = sender(json.loads(row['payload']))
        except Exception as exc:
            result = dict(ok=False, error=type(exc).__name__)
        with conn:
            if result.get('cancelled'):
                conn.execute("UPDATE notification_outbox SET state='cancelled',last_error=? WHERE event_key=?",
                             (result.get('error', 'ad_changed'), row['event_key']))
            elif result.get('ok'):
                conn.execute("""UPDATE notification_outbox SET state='sent',sent_ts=?,message_id=?,
                    attempts=attempts+1,last_error=NULL WHERE event_key=?""", (now, result.get('message_id'), row['event_key']))
                conn.execute("UPDATE lot_history SET notified=1 WHERE ad_id=?", (row['ad_id'],))
            else:
                delay = max(int(result.get('retry_after') or 0), min(1800, 30 * 2 ** min(row['attempts'], 6)))
                conn.execute("""UPDATE notification_outbox SET state='pending',attempts=attempts+1,
                    next_attempt_ts=?,last_error=? WHERE event_key=?""", (now + delay, result.get('error', 'telegram_failed'), row['event_key']))
                if result.get('drop_photo'):
                    payload = json.loads(row['payload'])
                    payload['photo_url'] = None
                    conn.execute("UPDATE notification_outbox SET payload=? WHERE event_key=?", (json.dumps(payload, ensure_ascii=False), row['event_key']))


def recover_outbox(conn):
    with conn:
        conn.execute("""UPDATE notification_outbox SET state='pending',last_error='delivery_unknown_after_restart'
            WHERE state='sending'""")


def mark_unavailable(conn, ad_id, observed_ts, evidence):
    """Unavailability is observable; sale, sale time and transaction price are unknown."""
    row = conn.execute("SELECT last_active_ts FROM lot_history WHERE ad_id=?", (ad_id,)).fetchone()
    last_active = row[0] if row else None
    conn.execute("""INSERT INTO availability_history
        (ad_id,observed_ts,last_active_ts,observation,evidence) VALUES (?,?,?,'unavailable',?)""",
        (ad_id, observed_ts, last_active, evidence))
    for table in ('lot_history', 'ml_ads'):
        conn.execute(f"""UPDATE {table} SET status='unavailable',unavailable_detected_ts=?,
            unavailable_reason=?,estimated_sold_price=NULL,days_on_market=NULL WHERE ad_id=?""",
            (observed_ts, evidence, ad_id))
    conn.execute("UPDATE ml_ads SET sold_ts=NULL WHERE ad_id=?", (ad_id,))
    conn.execute("""UPDATE notification_outbox SET state='cancelled',last_error='ad_unavailable'
        WHERE ad_id=? AND state='pending'""", (ad_id,))


def mark_active(conn, ad_id, now):
    for table in ('lot_history', 'ml_ads'):
        conn.execute(f"""UPDATE {table} SET last_active_ts=?,unavailable_detected_ts=NULL,
            unavailable_reason=NULL WHERE ad_id=?""", (now, ad_id))


def settings_revision(conn, settings):
    relevant = {k: v for k, v in settings.items() if k != 'last_digest_date'}
    digest = hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()
    row = conn.execute("SELECT value FROM meta WHERE key='settings_revision'").fetchone()
    with conn:
        if row and row[0] != digest:
            conn.execute("UPDATE feed_inbox SET state='pending',next_attempt_ts=0,attempts=0")
            conn.execute("UPDATE notification_outbox SET state='cancelled',last_error='settings_changed' WHERE state='pending'")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('settings_revision',?)", (digest,))


def watchdog(ready=False):
    address = os.getenv('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(b'READY=1\nWATCHDOG=1' if ready else b'WATCHDOG=1')
