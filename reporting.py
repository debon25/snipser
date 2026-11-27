"""Offline HTML report. Reads the supplied SQLite connection; makes no requests."""
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from string import Template
import time
from urllib.parse import quote


MINSK = timezone(timedelta(hours=3))


def stamp(value):
    return datetime.fromtimestamp(value, MINSK).strftime('%d.%m.%Y %H:%M') if value else '—'


def money(value):
    return f'{value:.0f} BYN' if value is not None else '—'


def model(row):
    name = row['model'] or row['title'] or row['ad_id']
    memory = f" ({row['memory']}GB)" if row['memory'] else ''
    url = 'https://www.kufar.by/item/' + quote(str(row['ad_id']), safe='')
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{escape(name)}{memory}</a>'


def description(row):
    title, body = (row['title'] or '').strip(), (row['description'] or '').strip()
    parts = [f'<b>{escape(title)}</b>'] if title else []
    if body:
        parts.append(escape(body[:260] + ('…' if len(body) > 260 else '')).replace('\n', ' '))
    return '<br>'.join(parts) or '<span class="muted">—</span>'


def age(row):
    start, end = row['list_time_ts'], row['last_active_ts']
    return f'{max(0, end - start) / 86400:.1f} дн.' if start and end else '—'


def empty_rows(rows, columns):
    return ''.join(rows) or f'<tr class="empty"><td colspan="{columns}" class="muted">Пока нет данных</td></tr>'


def generate_report(conn, settings, path, now=None):
    now = int(time.time() if now is None else now)
    counts = dict(conn.execute('SELECT status,COUNT(*) FROM lot_history GROUP BY status'))
    stats = dict(total=sum(counts.values()), active=counts.get('active', 0),
                 unavailable=counts.get('unavailable', 0))
    values = dict(stats, now=stamp(now), banned=counts.get('banned', 0), ignored=counts.get('ignored', 0))
    for key, query in {
        'dataset': 'SELECT COUNT(*) FROM ml_ads',
        'prices': 'SELECT COUNT(*) FROM price_history',
        'processing': "SELECT COUNT(*) FROM feed_inbox WHERE state='pending'",
        'sending': "SELECT COUNT(*) FROM notification_outbox WHERE state IN ('pending','sending')",
    }.items():
        values[key] = conn.execute(query).fetchone()[0]

    market_rows = []
    for row in conn.execute('SELECT * FROM market_cache ORDER BY updated_ts DESC,cache_key'):
        estimate = row['market_price']
        anchor = settings.get('anchors', {}).get(row['cache_key'])
        final = anchor if anchor else round(estimate * settings.get('bargain_factor', 1.0)) if estimate is not None else None
        badge = '<span class="badge blue">⚓ якорь</span>' if anchor else '<span class="badge gray">витрина</span>'
        median = f"{row['median_days']:.1f} дн." if row['median_days'] is not None else '—'
        market_rows.append(
            f"<tr><td><b>{escape(row['cache_key'].replace('_', ' '))}</b></td>"
            f"<td>{money(estimate)} <span class='muted'>(n={row['sample_size'] or 0})</span></td>"
            f"<td><b>{money(final)}</b> {badge}</td><td>{median}</td><td>{stamp(row['updated_ts'])}</td></tr>")

    unavailable_rows = []
    for row in conn.execute("""SELECT * FROM lot_history WHERE status='unavailable'
        AND unavailable_detected_ts>=? ORDER BY unavailable_detected_ts DESC,ad_id LIMIT 1000""", (now - 30 * 86400,)):
        legacy = row['unavailable_reason'] == 'legacy_inference'
        label = 'Старая отметка' if legacy else 'Недоступно'
        reason = 'Прежний вывод без подтверждения' if legacy else row['unavailable_reason'] or 'Причина неизвестна'
        battery = f"{row['battery_health']}%" if row['battery_health'] else '—'
        unavailable_rows.append(
            f"<tr><td>{stamp(row['unavailable_detected_ts'])}</td>"
            f"<td><span class='badge gray' title='{escape(reason, quote=True)}'>{label}</span></td>"
            f"<td>{model(row)}</td><td>{battery}</td><td>{money(row['last_seen_price'])}</td>"
            f"<td>{stamp(row['last_active_ts'])}</td><td>{age(row)}</td><td class='desc-cell'>{description(row)}</td></tr>")

    active_rows = []
    for row in conn.execute("SELECT * FROM lot_history WHERE status='active' ORDER BY last_seen_ts DESC,ad_id LIMIT 100"):
        battery = f"{row['battery_health']}%" if row['battery_health'] else '—'
        active_rows.append(
            f"<tr><td>{model(row)}</td><td>{battery}</td><td>{escape(row['district'] or '—')}</td>"
            f"<td>{money(row['last_seen_price'])}</td><td>{age(row)}</td><td class='desc-cell'>{description(row)}</td></tr>")

    values.update(model_rows=empty_rows(market_rows, 5), unavailable_rows=empty_rows(unavailable_rows, 8),
                  active_rows=empty_rows(active_rows, 6))
    template = Template(Path(__file__).with_name('report_template.html').read_text(encoding='utf-8'))
    Path(path).write_text(template.substitute(values), encoding='utf-8')
    return str(path), stats
