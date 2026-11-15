"""Offline end-to-end aggregation, schedule, email, and recovery checks."""
import json
import sqlite3
import statistics
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from analytics import weekly_report as report

UTC = timezone.utc


class Median:
    def __init__(self):
        self.values = []

    def step(self, value):
        if value is not None:
            self.values.append(value)

    def finalize(self):
        return statistics.median(self.values) if self.values else None


class SQLiteCursor:
    """Only adapt Databricks timestamp casts; run the actual aggregation SQL."""
    def __init__(self, connection):
        self.cursor = connection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, sql, params=None):
        if sql.startswith('SET TIME ZONE'):
            return
        for key in ('start', 'end', 'previous'):
            sql = sql.replace(f'CAST(:{key} AS TIMESTAMP)', f':{key}')
        self.cursor.execute(sql, params or {})

    @property
    def description(self):
        return self.cursor.description

    def fetchall(self):
        return self.cursor.fetchall()


class WeeklyReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = report.validate_config(dict(sender='reports@example.com',
                                                   recipients=['one@example.com', 'two@example.com'],
                                                   dashboard_url='https://example.com/dashboard'))
        self.now = datetime(2026, 11, 15, 14, tzinfo=UTC)  # Sunday 08:00 CST
        self.start, self.end = report.reporting_window(self.now, self.config)
        self.data = report.demo_report(self.now)
        self.rendered = report.render_report(self.data, self.start, self.end, self.now, self.config)

    def test_custom_schedule_and_latest_due_week(self):
        self.assertEqual(self.start.date(), date(2026, 11, 8))
        self.assertEqual(self.end.date(), date(2026, 11, 15))
        before = report.reporting_window(self.now - timedelta(seconds=1), self.config)
        self.assertEqual(before[1].date(), date(2026, 11, 8))
        missed = report.reporting_window(self.now + timedelta(days=3), self.config)
        self.assertEqual(missed, (self.start, self.end))
        custom = report.validate_config(dict(weekday='wednesday', time='19:45', timezone='Asia/Kolkata'))
        custom_now = datetime(2026, 11, 18, 14, 15, tzinfo=UTC)
        a, b = report.reporting_window(custom_now, custom)
        self.assertEqual(b.date(), date(2026, 11, 18))
        self.assertEqual(a.date(), date(2026, 11, 11))

    def test_dst_windows_use_local_calendar_days(self):
        for end, hours in [(date(2026, 11, 8), 169), (date(2026, 3, 15), 167)]:
            start, finish = report.reporting_window(self.now, self.config, end)
            self.assertEqual((finish.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600, hours)
            self.assertEqual((finish.date() - start.date()).days, 7)

    def test_validation_and_settings_wizard(self):
        for values in [dict(weekday='Sunday'), dict(time='25:00'), dict(recipients='person@example.com'),
                       dict(sender='a@example.com\nBcc: x@example.com'), dict(dashboard_url='javascript:alert(1)'),
                       dict(stale_after_hours=0), dict(stale_after_hours=float('nan')), dict(unrecognized=True)]:
            with self.subTest(values=values), self.assertRaises(report.ReportError):
                report.validate_config(values)
        path = self.root / 'config.json'
        with patch('builtins.input', side_effect=['Home', 'friday', '17:30', 'Europe/London',
                                                 'a@example.com,b@example.com', 's@example.com', 'https://example.com']):
            with patch('builtins.print'):
                report.configure(path)
        config = report.load_config(path)
        self.assertEqual(config['weekday'], 'friday')
        self.assertEqual(config['time'], '17:30')
        self.assertEqual(len(config['recipients']), 2)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sql_aggregation_boundaries_backlog_and_latency(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.create_aggregate('MEDIAN', 1, Median)
        conn.create_function('unix_timestamp', 1, lambda value: datetime.fromisoformat(value).timestamp() if value else None)
        conn.executescript('''CREATE TABLE incidents (
            incident_id TEXT, timestamp TEXT, event_type TEXT, room TEXT, status TEXT,
            review_outcome TEXT, alerted_at TEXT, acknowledged_at TEXT,
            escalations INT DEFAULT 0, delivery_failed BOOLEAN DEFAULT 0, alert_sent BOOLEAN DEFAULT 0);
            CREATE TABLE lifecycle_events (logged_at TEXT);''')

        def incident(identifier, timestamp, event='fall', status='resolved', room='Bedroom', **fields):
            row = dict(incident_id=identifier, timestamp=timestamp, event_type=event,
                       status=status, room=room, **fields)
            conn.execute('INSERT INTO incidents (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))

        incident('a', '2026-11-10T12:00:00+00:00', review_outcome='confirmed_fall',
                 alerted_at='2026-11-10T12:00:00+00:00', acknowledged_at='2026-11-10T12:00:48+00:00',
                 escalations=2, delivery_failed=1, alert_sent=1)
        incident('b', '2026-11-11T12:00:00+00:00', status='cancelled', room='')
        incident('c', '2026-11-12T12:00:00+00:00', event='near_fall', review_outcome='no_fall',
                 alerted_at='2026-11-12T12:00:00+00:00', acknowledged_at='2026-11-12T11:59:00+00:00', alert_sent=1)
        incident('offline', '2026-11-13T12:00:00+00:00', event='camera_offline', status='alerted',
                 alerted_at='2026-11-13T12:00:00+00:00', alert_sent=1)
        incident('hazard', '2026-11-14T12:00:00+00:00', event='obstacle')
        incident('old-open', '2026-10-01T12:00:00+00:00', status='alerted', alerted_at='2026-10-01T12:00:00+00:00')
        incident('old-review', '2026-10-01T12:00:00+00:00')
        incident('previous', '2026-11-07T12:00:00+00:00', review_outcome='unsure')
        incident('at-end', self.end.astimezone(UTC).isoformat(), event='obstacle')
        incident('at-start', self.start.astimezone(UTC).isoformat(), event='low_light')
        incident('unknown-time', None, event='obstacle')
        conn.execute('INSERT INTO lifecycle_events VALUES (?)', (self.now.isoformat(),))
        connection = MagicMock()
        connection.cursor.side_effect = lambda: SQLiteCursor(conn)
        with patch('analytics.ingest.TABLE', 'main.lifecycle_events'):
            data = report.fetch_report(self.start, self.end, connect=lambda: connection)
        c = data['current']
        self.assertEqual(c['incidents'], 6)
        self.assertEqual(c['falls'], 2)
        self.assertEqual(c['near_falls'], 1)
        self.assertEqual(c['confirmed'], 1)
        self.assertEqual(c['unreviewed'], 1)  # cancellation is not a no-fall review
        self.assertEqual(c['median_ack'], 48)
        self.assertEqual(c['ack_samples'], 1)  # negative duration excluded
        self.assertEqual(c['delivery_failures'], 1)
        self.assertEqual(c['unaccepted'], 0)  # successful retry is accepted
        self.assertEqual(c['offline'], 1)
        self.assertEqual(c['prevention'], 2)
        self.assertEqual(data['previous']['falls'], 1)
        self.assertEqual(data['backlog'], dict(unaccepted=1, unanswered=2, reviews=2, missing_timestamp=1))
        self.assertEqual(sum(r['detections'] for r in data['rooms']), 3)
        connection.close.assert_called_once()
        # Empty views still produce both periods and explicit unknown latency.
        conn.execute('DELETE FROM incidents')
        conn.execute('DELETE FROM lifecycle_events')
        with patch('analytics.ingest.TABLE', 'main.lifecycle_events'):
            empty = report.fetch_report(self.start, self.end, connect=lambda: connection)
        self.assertEqual(empty['current']['falls'], 0)
        self.assertIsNone(empty['current']['median_ack'])
        self.assertIsNone(empty['latest'])

    def test_query_failure_closes_connection(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.execute.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            report.fetch_report(self.start, self.end, connect=lambda: connection)
        connection.close.assert_called_once()

    def test_legacy_view_has_actionable_migration_error(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.execute.side_effect = [
            None, RuntimeError('[UNRESOLVED_COLUMN.WITH_SUGGESTION] review_outcome missing')]
        with self.assertRaisesRegex(report.ReportError, 'ingest.py --init'):
            report.fetch_report(self.start, self.end, connect=lambda: connection)
        connection.close.assert_called_once()

    def test_empty_stale_and_hostile_text(self):
        self.data['latest'] = None
        self.data['current'].update({k: 0 for k in self.data['current'] if k != 'median_ack'})
        self.data['current']['median_ack'] = None
        self.data['rooms'] = [dict(room='<img src=x onerror=alert(1)>', detections=1)]
        subject, text, html = report.render_report(self.data, self.start, self.end, self.now, self.config)
        self.assertIn('No incidents were recorded', text)
        self.assertIn('recent data is unavailable', text)
        self.assertIn('Not available', text)
        self.assertIn('&lt;img', html)
        self.assertNotIn('<img src=x', html)
        self.assertIn('Data may be incomplete', html)
        self.assertIn('https://example.com/dashboard', html)
        self.assertIn('Nov 08 – Nov 14, 2026', subject)
        self.data['latest'] = self.now - timedelta(hours=25)
        self.assertIn('Data may be incomplete', report.render_report(self.data, self.start, self.end, self.now, self.config)[2])

    def test_multipart_private_delivery_idempotency_and_new_recipient(self):
        state = self.root / 'ledger.sqlite3'
        send = MagicMock()
        self.assertEqual(report.deliver(self.rendered, self.start, self.end, self.config, state, self.now, send), 2)
        self.assertEqual(report.deliver(self.rendered, self.start, self.end, self.config, state, self.now, send), 0)
        first = send.call_args_list[0].args[0]
        self.assertEqual(first['To'], 'one@example.com')
        self.assertNotIn('two@example.com', first.as_string())
        self.assertEqual([p.get_content_type() for p in first.iter_parts()], ['text/plain', 'text/html'])
        self.assertIn('one@example.com', send.call_args_list[0].args)
        self.config['recipients'].append('three@example.com')
        self.assertEqual(report.deliver(self.rendered, self.start, self.end, self.config, state, self.now, send), 1)
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        # Next due week is a new delivery identity.
        self.assertEqual(report.deliver(self.rendered, self.start + timedelta(days=7), self.end + timedelta(days=7),
                                        self.config, state, self.now, send), 3)

    def test_partial_failure_preserves_success_and_requires_explicit_retry(self):
        state = self.root / 'ledger.sqlite3'
        send = MagicMock(side_effect=[TimeoutError('ambiguous'), None])
        with self.assertRaises(report.ReportError):
            report.deliver(self.rendered, self.start, self.end, self.config, state, self.now, send)
        with closing(sqlite3.connect(state)) as db:
            self.assertEqual(sorted(r[0] for r in db.execute('SELECT status FROM deliveries')), ['pending', 'sent'])
        again = MagicMock()
        with self.assertRaises(report.ReportError):
            report.deliver(self.rendered, self.start, self.end, self.config, state, self.now, again)
        again.assert_not_called()
        self.assertEqual(report.deliver(self.rendered, self.start, self.end, self.config, state, self.now,
                                        again, retry_uncertain=True), 1)
        self.assertEqual(send.call_args_list[0].args[0]['Message-ID'], again.call_args.args[0]['Message-ID'])

    def test_concurrent_invocation_observes_pending_intent(self):
        state = self.root / 'ledger.sqlite3'
        self.config['recipients'] = ['one@example.com']

        def sending(*args):
            other_send = MagicMock()
            with self.assertRaises(report.ReportError):
                report.deliver(self.rendered, self.start, self.end, self.config, state, self.now, other_send)
            other_send.assert_not_called()

        self.assertEqual(report.deliver(self.rendered, self.start, self.end, self.config, state, self.now, sending), 1)

    def test_smtp_requires_tls_and_explicit_envelope(self):
        message = MagicMock()
        message.__getitem__.return_value = 'sender@example.com'
        with patch.dict('os.environ', dict(SMTP_HOST='smtp.example.com', SMTP_SECURITY='starttls',
                                          SMTP_USERNAME='user', SMTP_PASSWORD='secret'), clear=True), patch.object(report.smtplib, 'SMTP') as factory:
            smtp = factory.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            report.smtp_send(message, 'one@example.com')
            smtp.starttls.assert_called_once()
            smtp.login.assert_called_once_with('user', 'secret')
            smtp.send_message.assert_called_once_with(message, from_addr='sender@example.com', to_addrs=['one@example.com'])
        with patch.dict('os.environ', dict(SMTP_HOST='smtp.example.com', SMTP_SECURITY='ssl'), clear=True), patch.object(report.smtplib, 'SMTP_SSL') as factory:
            factory.return_value.__enter__.return_value.send_message.return_value = {}
            report.smtp_send(message, 'one@example.com')
            self.assertEqual(factory.call_args.kwargs['port'], 465)
            self.assertIn('context', factory.call_args.kwargs)
        with patch.dict('os.environ', dict(SMTP_HOST='smtp.example.com', SMTP_SECURITY='none'), clear=True):
            with self.assertRaises(report.ReportError):
                report.smtp_send(message, 'one@example.com')
        # Invalid SMTP config must not create pending delivery records.
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(report.ReportError):
            report.deliver(self.rendered, self.start, self.end, self.config, self.root / 'not-created.sqlite3', self.now)
        self.assertFalse((self.root / 'not-created.sqlite3').exists())

    def test_demo_preview_is_offline_and_does_not_deliver(self):
        output = self.root / 'preview.html'
        with patch('sys.argv', ['weekly_report.py', '--demo', '--config', str(self.root / 'missing.json'), '--output', str(output)]), patch.object(report, 'fetch_report') as fetch, patch.object(report, 'deliver') as deliver, patch('builtins.print'):
            report.main()
        fetch.assert_not_called()
        deliver.assert_not_called()
        self.assertIn('SAMPLE DATA', output.read_text())
        self.assertIn('SAMPLE DATA', output.with_suffix('.txt').read_text())

    def test_dashboard_uses_same_review_source(self):
        dashboard = json.loads((Path(__file__).parents[1] / 'dashboard/SafeStep.lvdash.json').read_text())
        self.assertEqual(dashboard['datasets'][0]['config']['source'], 'SELECT * FROM workspace.safestep.incidents')


if __name__ == '__main__':
    unittest.main()
