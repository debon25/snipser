"""Read-only data readiness report. Does not claim sale-price accuracy."""
import argparse
import json
import os
from pathlib import Path
import time
from state_store import readonly, describe_db


def audit(path, now=None):
    now = int(now or time.time())
    counts = describe_db(path)
    with readonly(path) as conn:
        conn.row_factory = __import__('sqlite3').Row
        columns = {row[1] for row in conn.execute('PRAGMA table_info(ml_ads)')}
        confirmed_expr = 'SUM(confirmed_sale_price IS NOT NULL)' if 'confirmed_sale_price' in columns else '0'
        legacy_expr = 'SUM(legacy_estimated_sold_price IS NOT NULL)' if 'legacy_estimated_sold_price' in columns else 'SUM(estimated_sold_price IS NOT NULL)'
        statuses = dict(conn.execute('SELECT status,COUNT(*) FROM ml_ads GROUP BY status').fetchall())
        quality = dict(conn.execute(f"""SELECT COUNT(*) total,
            SUM(model IS NOT NULL) recognized,
            SUM(model IS NOT NULL AND COALESCE(is_assumed_mem,1)=1) assumed_memory,
            SUM(body IS NULL OR body='') missing_description,
            SUM(model LIKE 'iPhone%' AND battery_health IS NULL) iphones_missing_battery,
            SUM(status='active' AND last_seen_ts<?) stale_active,
            {confirmed_expr} confirmed_prices,
            {legacy_expr} legacy_heuristic_prices
            FROM ml_ads""", (now - 86400,)).fetchone())
        groups = [dict(row) for row in conn.execute("""SELECT model,memory,COUNT(*) sample,
            MIN(price_byn) min_asking,MAX(price_byn) max_asking
            FROM ml_ads WHERE status='active' AND model IS NOT NULL
            AND COALESCE(is_assumed_mem,1)=0 AND COALESCE(has_defects,1)=0
            AND last_seen_ts>=? GROUP BY model,memory ORDER BY sample DESC""", (now-86400,))]
        backlog = dict(conn.execute('SELECT state,COUNT(*) FROM feed_inbox GROUP BY state').fetchall()) if 'feed_inbox' in counts else {}
    return dict(checked_ts=now, rows=counts, statuses=statuses, quality=quality,
                migration_required='confirmed_sale_price' not in columns,
                fresh_groups_without_detected_defects=groups, processing_queue=backlog,
                condition_note='No defect marker is not a physical inspection or confirmation of working condition.',
                conclusion='Sale-price accuracy cannot be measured from asking prices or disappearances. Confirmed transactions and predictions frozen before sale are required.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=os.getenv('DB_FILE', 'sniper.db'))
    parser.add_argument('--out')
    args = parser.parse_args()
    result = json.dumps(audit(args.db), ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(result, encoding='utf-8')
    else:
        print(result)
