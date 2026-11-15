#!/usr/bin/env python3
"""Preview or deliver the SafeStep weekly dashboard digest. See reports/README.md."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import smtplib
import sqlite3
import ssl
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from html import escape
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
UTC = timezone.utc
DAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')


class ReportError(Exception):
    """A safe, actionable error message that contains no credentials."""


DEFAULTS = dict(name='SafeStep', timezone='America/Chicago', weekday='sunday',
                time='08:00', recipients=[], sender='', dashboard_url='', stale_after_hours=24)


def validate_config(values):
    if not isinstance(values, dict):
        raise ReportError('Report settings must be a JSON object')
    unknown = values.keys() - DEFAULTS.keys()
    if unknown:
        raise ReportError('Unknown settings: ' + ', '.join(sorted(unknown)))
    config = {**DEFAULTS, **values}
    ZoneInfo(config['timezone'])
    if config['weekday'] not in DAYS or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', config['time']):
        raise ReportError('Choose a weekday (lowercase) and a 24-hour HH:MM time')
    if not isinstance(config['name'], str) or not config['name'].strip() or '\n' in config['name'] or '\r' in config['name']:
        raise ReportError('name must be a non-empty single line')
    if not isinstance(config['recipients'], list):
        raise ReportError('recipients must be a list of email addresses')
    for address in config['recipients'] + ([config['sender']] if config['sender'] else []):
        if not isinstance(address, str) or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', address):
            raise ReportError('Use plain email addresses for sender and recipients')
    config['recipients'] = list(dict.fromkeys(config['recipients']))
    url = urlparse(config['dashboard_url'])
    if config['dashboard_url'] and (url.scheme != 'https' or not url.netloc or url.username or url.password):
        raise ReportError('dashboard_url must be an HTTPS URL without credentials')
    if isinstance(config['stale_after_hours'], bool) or not isinstance(config['stale_after_hours'], (int, float)) or not 0 < config['stale_after_hours'] < 8760:
        raise ReportError('stale_after_hours must be between 0 and 8760')
    return config


def load_config(path):
    return validate_config(json.loads(path.read_text()))


def configure(path):
    """A small settings wizard; SMTP secrets stay in environment variables."""
    config = load_config(path) if path.exists() else DEFAULTS.copy()
    for key, prompt in [('name', 'Report name'), ('weekday', 'Send day (monday–sunday)'),
                        ('time', 'Send time (HH:MM, 24 hour)'), ('timezone', 'Timezone (e.g. America/Chicago)'),
                        ('recipients', 'Recipients (comma-separated)'), ('sender', 'From email (custom SMTP only; leave blank for Databricks snapshots)'),
                        ('dashboard_url', 'Published dashboard HTTPS link')]:
        current = ', '.join(config[key]) if key == 'recipients' else config[key]
        answer = input(f'{prompt} [{current}]: ').strip()
        if answer:
            config[key] = [s.strip() for s in answer.split(',')] if key == 'recipients' else answer
    config = validate_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + '\n')
    path.chmod(0o600)
    print(f'Saved {path}. SMTP credentials are configured separately; see reports/README.md.')


def reporting_window(now, config, end=None):
    """Local calendar boundaries, converted to UTC only when querying."""
    local = now.astimezone(ZoneInfo(config['timezone']))
    if end is None:
        end = local.date() - timedelta(days=(local.weekday() - DAYS.index(config['weekday'])) % 7)
        due = datetime.combine(end, time.fromisoformat(config['time']), local.tzinfo)
        if local < due:
            end -= timedelta(days=7)
    end_at = datetime.combine(end, time.min, local.tzinfo)
    return end_at - timedelta(days=7), end_at


METRICS_SQL = """
WITH periods AS (
  SELECT 'current' AS period, CAST(:start AS TIMESTAMP) AS start_at, CAST(:end AS TIMESTAMP) AS end_at
  UNION ALL SELECT 'previous', CAST(:previous AS TIMESTAMP), CAST(:start AS TIMESTAMP)
)
SELECT p.period, COUNT(i.incident_id) AS incidents,
       COUNT(CASE WHEN event_type = 'fall' THEN 1 END) AS falls,
       COUNT(CASE WHEN event_type = 'near_fall' THEN 1 END) AS near_falls,
       COUNT(CASE WHEN event_type IN ('fall','near_fall') AND review_outcome = 'confirmed_fall' THEN 1 END) AS confirmed,
       COUNT(CASE WHEN event_type IN ('fall','near_fall') AND review_outcome = 'no_fall' THEN 1 END) AS no_fall,
       COUNT(CASE WHEN event_type IN ('fall','near_fall') AND review_outcome = 'unsure' THEN 1 END) AS unsure,
       COUNT(CASE WHEN event_type IN ('fall','near_fall') AND review_outcome IS NULL THEN 1 END) AS unreviewed,
       COUNT(CASE WHEN alerted_at IS NOT NULL THEN 1 END) AS alerts,
       COUNT(CASE WHEN alerted_at IS NOT NULL AND acknowledged_at IS NOT NULL THEN 1 END) AS acknowledged,
       COUNT(CASE WHEN escalations > 0 THEN 1 END) AS escalated,
       COUNT(CASE WHEN delivery_failed THEN 1 END) AS delivery_failures,
       COUNT(CASE WHEN alerted_at IS NOT NULL AND NOT alert_sent THEN 1 END) AS unaccepted,
       COUNT(CASE WHEN event_type = 'camera_offline' THEN 1 END) AS offline,
       COUNT(CASE WHEN event_type IN ('obstacle','low_light','night_wandering','inactivity') THEN 1 END) AS prevention,
       MEDIAN(CASE WHEN acknowledged_at >= alerted_at THEN unix_timestamp(acknowledged_at) - unix_timestamp(alerted_at) END) AS median_ack,
       COUNT(CASE WHEN acknowledged_at >= alerted_at THEN 1 END) AS ack_samples
FROM periods p LEFT JOIN {namespace}.incidents i ON i.`timestamp` >= p.start_at AND i.`timestamp` < p.end_at
GROUP BY p.period
"""
ROOMS_SQL = """
SELECT COALESCE(NULLIF(TRIM(room), ''), 'Room not recorded') AS room,
       COUNT(*) AS detections
FROM {namespace}.incidents
WHERE `timestamp` >= CAST(:start AS TIMESTAMP) AND `timestamp` < CAST(:end AS TIMESTAMP)
  AND event_type IN ('fall', 'near_fall')
GROUP BY 1 ORDER BY detections DESC, room LIMIT 3
"""
BACKLOG_SQL = """
SELECT COUNT(CASE WHEN alerted_at IS NOT NULL AND NOT alert_sent AND status NOT IN ('cancelled','resolved') THEN 1 END) AS unaccepted,
       COUNT(CASE WHEN alerted_at IS NOT NULL AND acknowledged_at IS NULL AND status NOT IN ('cancelled','resolved') THEN 1 END) AS unanswered,
       COUNT(CASE WHEN event_type IN ('fall','near_fall') AND status IN ('cancelled','resolved') AND review_outcome IS NULL THEN 1 END) AS reviews,
       COUNT(CASE WHEN `timestamp` IS NULL THEN 1 END) AS missing_timestamp
FROM {namespace}.incidents
"""


def fetch_report(start, end, connect=None, namespace=None):
    # Reuse ingestion's validated destination and TLS settings, never touch its checkpoint.
    if namespace is None or connect is None:
        try:
            from .ingest import TABLE, _connect
        except ImportError:
            from ingest import TABLE, _connect
        namespace = namespace or TABLE.rsplit('.', 1)[0]
        connect = connect or _connect
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?', namespace):
        raise ReportError('Invalid report namespace')
    params = {key: value.astimezone(UTC).isoformat() for key, value in
              [('start', start), ('end', end), ('previous', start - timedelta(days=7))]}
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")

            def query(sql, bindings=None):
                try:
                    cursor.execute(sql.format(namespace=namespace), bindings or {})
                except Exception as exc:
                    message = str(exc)
                    if 'UNRESOLVED_COLUMN' in message and any(field in message for field in ('review_outcome', 'review_note', 'reviewed_by', 'reviewed_at')):
                        raise ReportError('The Databricks incident view is missing caregiver-review fields. Apply the current analytics/sql/schema.sql view via analytics/ingest.py --init (inspect --init --dry-run first).') from None
                    raise
                columns = [col[0] for col in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]

            metrics = query(METRICS_SQL, params)
            rooms = query(ROOMS_SQL, {k: params[k] for k in ('start', 'end')})
            backlog = query(BACKLOG_SQL)[0]
            latest = query('SELECT MAX(logged_at) AS latest FROM {namespace}.lifecycle_events')[0]['latest']
    finally:
        connection.close()
    periods = {row.pop('period'): row for row in metrics}
    return {**periods, 'rooms': rooms, 'backlog': backlog, 'latest': latest}


def demo_report(end):
    return dict(current=dict(incidents=28, falls=4, near_falls=6, confirmed=2, no_fall=5,
                            unsure=1, unreviewed=2, alerts=12, acknowledged=10, escalated=2,
                            delivery_failures=1, unaccepted=0, offline=1, prevention=17,
                            median_ack=48, ack_samples=10),
                previous=dict(incidents=32, falls=6, near_falls=8, median_ack=65, ack_samples=12),
                rooms=[dict(room='Bedroom', detections=5), dict(room='Living room', detections=3),
                       dict(room='Kitchen', detections=2)],
                backlog=dict(unaccepted=0, unanswered=1, reviews=2, missing_timestamp=0), latest=end)


def duration(value):
    if value is None:
        return 'Not available'
    seconds = round(float(value))
    return f'{seconds}s' if seconds < 60 else f'{seconds // 60}m {seconds % 60:02d}s'


def quantity(count, singular, plural=None):
    return f'{count} {singular if count == 1 else (plural or singular + "s")}'


def change(current, previous):
    delta = current - previous
    return 'No change vs prior week' if delta == 0 else f'{delta:+d} vs prior week ({previous})'


def render_report(data, start, end, now, config, demo=False):
    c, p, b = data['current'], data['previous'], data['backlog']
    label = f'{start:%b %d} – {end - timedelta(days=1):%b %d, %Y}'
    latest = data['latest']
    if isinstance(latest, str):
        latest = datetime.fromisoformat(latest.replace('Z', '+00:00'))
    if latest and latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)  # SQL session explicitly uses UTC.
    stale = latest is None or (now - latest).total_seconds() > config['stale_after_hours'] * 3600
    freshness = ('No lifecycle data available.' if latest is None else
                 f'Latest lifecycle event: {latest.astimezone(ZoneInfo(config["timezone"])):%b %d, %Y %H:%M %Z}.')
    actions = []
    if stale:
        actions.append('Check analytics ingestion and camera connectivity: recent data is unavailable. A quiet event log does not prove monitoring coverage.')
    if b['unaccepted']:
        actions.append(f'Check notification delivery: {quantity(b["unaccepted"], "open incident has", "open incidents have")} no alert accepted by ntfy.')
    if b['unanswered']:
        actions.append(f'Check the response queue: {quantity(b["unanswered"], "open alert has", "open alerts have")} no caregiver acknowledgement.')
    if b['reviews']:
        actions.append(f'Record a review when known: {quantity(b["reviews"], "closed fall / near-fall detection awaits", "closed fall / near-fall detections await")} an outcome.')
    if b['missing_timestamp']:
        actions.append(f'Check source data: {quantity(b["missing_timestamp"], "incident has", "incidents have")} no detection timestamp and cannot appear in weekly trends.')
    if not actions:
        actions.append('No outstanding items in the tracked delivery, acknowledgement, and review queues at generation time.')
    headline = (f'{quantity(c["falls"], "possible fall detection")}, {quantity(c["near_falls"], "near-fall detection")}, '
                f'and {quantity(c["confirmed"], "caregiver-reported confirmed fall")}.')
    if c['incidents'] == 0:
        headline = 'No incidents were recorded for this reporting week. This does not establish that no events occurred or that monitoring was active.'
    tiles = [('Possible falls', str(c['falls']), change(c['falls'], p['falls'])),
             ('Near-fall detections', str(c['near_falls']), change(c['near_falls'], p['near_falls'])),
             ('Caregiver-confirmed', str(c['confirmed']), 'Across fall / near-fall detections'),
             ('Median response', duration(c['median_ack']), f'{c["ack_samples"]} valid samples; prior {duration(p["median_ack"])} ({p["ack_samples"]} samples)')]
    outcomes = f'Caregiver reviews: {c["confirmed"]} confirmed fall · {c["no_fall"]} no fall · {c["unsure"]} unsure · {c["unreviewed"]} unreviewed.'
    operations = [
        f'Response: {c["acknowledged"]} of {c["alerts"]} alerted incidents acknowledged; {c["escalated"]} escalated.',
        f'Notifications: {c["delivery_failures"]} incidents with a failed attempt, including retries; {c["unaccepted"]} with no alert accepted by ntfy.',
        f'Monitoring: {quantity(c["offline"], "camera-offline episode")}.',
        f'Prevention: {quantity(c["prevention"], "signal")} from obstacles, low light, night wandering, or inactivity.']
    notes = ('Detections are not verified injuries. Caregiver reviews are reported observations; cancellation does not establish no fall. '
             'Response measures first alert decision to first acknowledgement, excluding negative durations. '
             'ntfy acceptance does not confirm phone receipt. Latest reviews and statuses can change earlier counts. '
             'Lifecycle activity does not establish uptime. Weekly email complements immediate alerts.')
    text = '\n\n'.join(([ 'SAMPLE DATA — preview only' ] if demo else []) + [
        f'{config["name"]} | Weekly safety report | {label}', headline,
        'AT A GLANCE\n' + '\n'.join(f'{a}: {v} — {s}' for a, v, s in tiles),
        'FOLLOW UP NOW (all dates; latest status)\n' + '\n'.join(f'• {a}' for a in actions),
        'FALL / NEAR-FALL LOCATIONS\n' + ('\n'.join(f'{r["room"]}: {r["detections"]}' for r in data['rooms']) or 'No fall / near-fall detections recorded.'),
        outcomes, 'OPERATIONS\n' + '\n'.join(operations), 'Full dashboard: ' + (config['dashboard_url'] or 'Link not configured.'),
        freshness + (' Data may be incomplete.' if stale else ''),
        f'Week uses {config["timezone"]}. Generated {now.astimezone(ZoneInfo(config["timezone"])):%b %d, %Y %H:%M %Z}.', notes])
    e = lambda value: escape(str(value), quote=True)
    tile_html = ''.join(f'<td width="50%" valign="top" style="padding:14px;border:1px solid #dfe6eb"><div style="font-size:12px;color:#526675">{e(a)}</div><div style="font-size:30px;font-weight:bold;color:#112f42;padding:5px 0">{e(v)}</div><div style="font-size:12px;color:#526675">{e(s)}</div></td>' for a, v, s in tiles)
    # Two cards per row remain readable in narrow email clients without CSS support.
    split = tile_html.find('</td>', tile_html.find('</td>') + 5) + 5
    tile_html = '<tr>' + tile_html[:split] + '</tr><tr>' + tile_html[split:] + '</tr>'
    room_html = ''.join(f'<tr><td style="padding:8px 0;border-bottom:1px solid #e5ebef">{e(r["room"])}</td><td align="right" style="border-bottom:1px solid #e5ebef">{e(r["detections"])}</td></tr>' for r in data['rooms'])
    button = (f'<p style="margin:26px 0"><a href="{e(config["dashboard_url"])}" style="background:#087e83;color:#ffffff;padding:13px 20px;border-radius:6px;text-decoration:none;display:inline-block;font-weight:bold">Open full dashboard →</a></p>' if config['dashboard_url'] else '<p>Dashboard link not configured.</p>')
    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Weekly SafeStep report</title></head>
<body style="margin:0;background:#edf3f5;font-family:Arial,Helvetica,sans-serif;color:#253c4b;line-height:1.5">
<div style="display:none;max-height:0;overflow:hidden">{e(headline)}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;background:#ffffff;border:1px solid #dfe6eb;border-radius:10px">
<tr><td style="background:#112f42;color:#ffffff;padding:26px;border-radius:10px 10px 0 0"><div style="font-size:12px;letter-spacing:2px;color:#8ae1d4">{e(config['name'].upper())} / WEEKLY DIGEST</div><h1 style="font-size:27px;line-height:1.2;margin:12px 0">Your week at a glance</h1><div>{e(label)} · {e(config['timezone'])}</div></td></tr>
<tr><td style="padding:26px">{'<p style="color:#9b4313;font-weight:bold">SAMPLE DATA — preview only</p>' if demo else ''}<p style="margin-top:0">{e(headline)}</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{tile_html}</table>
<h2 style="font-size:18px;margin:26px 0 4px">Follow up now</h2><div style="font-size:12px;color:#526675">All dates · latest status at generation time</div><ul style="padding-left:20px">{''.join('<li style="margin:10px 0">' + e(a) + '</li>' for a in actions)}</ul>
<h2 style="font-size:18px;margin:26px 0 4px">Where detections occurred</h2><div style="font-size:12px;color:#526675">Top rooms · fall and near-fall detections this week</div><table width="100%" cellspacing="0" cellpadding="0">{room_html or '<tr><td style="padding:10px 0">No fall / near-fall detections recorded.</td></tr>'}</table>
<h2 style="font-size:18px;margin:26px 0 4px">Reviews &amp; response</h2><p>{e(outcomes)}</p><ul style="padding-left:20px;font-size:14px">{''.join('<li style="margin:8px 0">' + e(item) + '</li>' for item in operations)}</ul>{button}
<div style="padding:12px;background:{'#fff4e6' if stale else '#edf7f5'};font-size:12px">{e(freshness)}{' <strong>Data may be incomplete.</strong>' if stale else ''}<br>Generated {e(now.astimezone(ZoneInfo(config['timezone'])).strftime('%b %d, %Y %H:%M %Z'))}.</div>
<p style="font-size:11px;color:#526675;margin-bottom:0">{e(notes)}</p></td></tr></table></td></tr></table></body></html>'''
    return f'{config["name"]} weekly report · {label}', text, html


def smtp_settings():
    host = os.environ.get('SMTP_HOST', '')
    mode = os.environ.get('SMTP_SECURITY', 'starttls')
    if not host or mode not in ('starttls', 'ssl'):
        raise ReportError('Set SMTP_HOST and SMTP_SECURITY=starttls or ssl')
    user, password = os.getenv('SMTP_USERNAME'), os.getenv('SMTP_PASSWORD')
    if bool(user) != bool(password):
        raise ReportError('Set both SMTP_USERNAME and SMTP_PASSWORD, or neither for a trusted relay')
    try:
        port = int(os.environ.get('SMTP_PORT', '465' if mode == 'ssl' else '587'))
    except ValueError:
        raise ReportError('SMTP_PORT must be an integer') from None
    if not 1 <= port <= 65535:
        raise ReportError('SMTP_PORT must be between 1 and 65535')
    return host, mode, user, password, port


def smtp_send(message, recipient):
    host, mode, user, password, port = smtp_settings()
    context = ssl.create_default_context()
    factory = smtplib.SMTP_SSL if mode == 'ssl' else smtplib.SMTP
    kwargs = dict(host=host, port=port, timeout=30)
    if mode == 'ssl':
        kwargs['context'] = context
    with factory(**kwargs) as smtp:
        if mode == 'starttls':
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if user:
            smtp.login(user, password)
        refused = smtp.send_message(message, from_addr=message['From'], to_addrs=[recipient])
        if refused:
            raise ReportError('SMTP server refused delivery')


def build_message(rendered, config, recipient, key, now):
    message = EmailMessage()
    message['Subject'], message['From'], message['To'] = rendered[0], config['sender'], recipient
    message['Date'] = format_datetime(now)
    message['Message-ID'] = f'<safestep-weekly-{key}@{config["sender"].split("@")[1]}>'
    message.set_content(rendered[1], cte='quoted-printable')
    message.add_alternative(rendered[2], subtype='html', cte='quoted-printable')
    return message


def validate_delivery(config):
    if not config['sender'] or not config['recipients'] or not config['dashboard_url']:
        raise ReportError('Set sender, recipients, and dashboard_url before sending')


def deliver(rendered, start, end, config, state, now, send=smtp_send, retry_uncertain=False):
    """Commit an intent before SMTP. Ambiguous results require deliberate retry."""
    validate_delivery(config)
    if send is smtp_send:
        smtp_settings()  # Validate before recording any delivery intent.
    state.parent.mkdir(parents=True, exist_ok=True)
    sent = 0
    failures = []
    with closing(sqlite3.connect(state, timeout=35)) as db:
        state.chmod(0o600)
        db.execute('CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, status TEXT, updated TEXT)')
        for recipient in config['recipients']:
            # Recipient-specific messages keep other caregivers' addresses private.
            key = hashlib.sha256(json.dumps([config['name'], config['timezone'], start.isoformat(), end.isoformat(), recipient]).encode()).hexdigest()
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM deliveries WHERE key=?', (key,)).fetchone()
            if row and row[0] == 'sent':
                db.commit()
                continue
            if row and not retry_uncertain:
                db.commit()
                failures.append('A previous delivery has an uncertain outcome; inspect SMTP logs before --retry-uncertain')
                continue
            message = build_message(rendered, config, recipient, key, now)
            db.execute('INSERT OR REPLACE INTO deliveries VALUES (?, ?, ?)', (key, 'pending', now.isoformat()))
            db.commit()
            try:
                send(message, recipient)
            except Exception:
                # Do not auto-retry SMTP DATA timeouts: the server may have accepted it.
                failures.append('Delivery failed or is uncertain; inspect SMTP logs before --retry-uncertain')
                continue
            db.execute('UPDATE deliveries SET status=?, updated=? WHERE key=?', ('sent', now.isoformat(), key))
            db.commit()
            sent += 1
    if failures:
        raise ReportError(f'{sent} sent; {len(failures)} delivery issue(s). ' + failures[0])
    return sent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=HERE / 'reports/config.json')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--configure', action='store_true', help='interactive schedule and recipient settings')
    mode.add_argument('--preview', action='store_true', help='query live data and write HTML/text; never send')
    mode.add_argument('--demo', action='store_true', help='preview sample data offline; never send')
    mode.add_argument('--send-due', action='store_true', help='send the latest due report once per recipient')
    parser.add_argument('--week-ending', type=date.fromisoformat, help='exclusive local end date; previews only')
    parser.add_argument('--output', type=Path, default=HERE / 'reports/preview.html')
    parser.add_argument('--state', type=Path, default=HERE / 'reports/delivery.sqlite3')
    parser.add_argument('--retry-uncertain', action='store_true', help='retry ambiguous deliveries after checking SMTP logs')
    args = parser.parse_args()
    if args.week_ending and args.send_due:
        parser.error('--week-ending is only allowed with --preview or --demo')
    if args.retry_uncertain and not args.send_due:
        parser.error('--retry-uncertain requires --send-due')
    if args.configure:
        configure(args.config)
        return
    config = load_config(args.config) if args.config.exists() else validate_config({})
    if args.send_due:
        validate_delivery(config)
        smtp_settings()
    now = datetime.now(UTC)
    start, end = reporting_window(now, config, args.week_ending)
    if args.demo and not config['dashboard_url']:
        config['dashboard_url'] = 'https://example.com/safestep-dashboard'
    data = demo_report(min(end, now)) if args.demo else fetch_report(start, end)
    rendered = render_report(data, start, end, now, config, demo=args.demo)
    if not args.send_due:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered[2])
        args.output.chmod(0o600)
        plain = args.output.with_suffix('.txt')
        plain.write_text(rendered[1])
        plain.chmod(0o600)
        print(f'Preview: {args.output.resolve()}\nPlain text: {plain.resolve()}')
    else:
        count = deliver(rendered, start, end, config, args.state, now, retry_uncertain=args.retry_uncertain)
        print(f'{count} report(s) accepted by SMTP; remaining recipients already sent.')


if __name__ == '__main__':
    try:
        main()
    except ReportError as exc:
        logging.error('%s', exc)
        raise SystemExit(1) from None
    except Exception as exc:
        # Connector / SMTP exception strings can contain recipients or credentials.
        logging.error('Weekly report stopped (%s). Check configuration, SQL access, and delivery state; see reports/README.md.', type(exc).__name__)
        raise SystemExit(1) from None
