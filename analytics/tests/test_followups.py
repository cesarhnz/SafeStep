"""Detector follow-ups (recovered / immobility) in the incident view, via SQLite."""
import json
import sqlite3
import unittest
from pathlib import Path

from analytics import weekly_report
from test_ingest import ingest


def view_conn():
    conn = sqlite3.connect(':memory:')
    conn.create_function('get_json_object', 2, lambda raw, path: json.loads(raw).get(path[2:]))
    conn.execute('CREATE TABLE lifecycle_events (' + ', '.join(
        name + ' ' + typ for name, typ in ingest.COLUMNS) + ')')
    schema = (Path(__file__).parents[1] / 'sql/schema.sql').read_text()
    view = schema[schema.index('CREATE OR REPLACE VIEW'):]
    conn.executescript(view.replace('CREATE OR REPLACE VIEW', 'CREATE VIEW').replace('safestep.', ''))
    return conn


def insert(conn, incident_id, rows):
    for row in rows:
        record = ingest.row_to_record(json.dumps({'incident_id': incident_id, 'type': 'fall', **row}).encode())
        conn.execute('INSERT INTO lifecycle_events VALUES (' + ','.join('?' for _ in record) + ')',
                     tuple(record.values()))


class FollowUpViewTests(unittest.TestCase):
    def test_recovered_and_immobility_timestamps_and_status(self):
        conn = view_conn()
        self.addCleanup(conn.close)
        insert(conn, 'UP', [
            {'lifecycle': 'detected', 'logged_at': '2026-11-15T10:00:00Z', 'status': 'pending_confirmation', 'confidence': 0.78},
            {'lifecycle': 'alerted', 'logged_at': '2026-11-15T10:00:30Z', 'status': 'alerted'},
            {'lifecycle': 'recovered', 'logged_at': '2026-11-15T10:00:40Z', 'status': 'recovered', 'by': 'SafeStep detector',
             'raw_event': {'metrics': {'recovery_seconds': 38.0}}},
        ])
        insert(conn, 'DOWN', [
            {'lifecycle': 'detected', 'logged_at': '2026-11-15T11:00:00Z', 'status': 'pending_confirmation'},
            {'lifecycle': 'immobility', 'logged_at': '2026-11-15T11:01:00Z', 'status': 'pending_confirmation',
             'immobility_seconds': 60.0},
            {'lifecycle': 'alerted', 'logged_at': '2026-11-15T11:01:00.001Z', 'status': 'alerted', 'immobility_seconds': 60.0},
        ])
        rows = {r[0]: r[1:] for r in conn.execute(
            'SELECT incident_id, status, recovered_at, immobility_reported_at, immobility_seconds, confidence '
            'FROM incidents')}
        status, recovered_at, immobility_at, immobility_s, _ = rows['UP']
        self.assertEqual(status, 'recovered')
        self.assertIsNotNone(recovered_at)
        self.assertIsNone(immobility_at)
        status, recovered_at, immobility_at, immobility_s, _ = rows['DOWN']
        self.assertEqual((status, immobility_s), ('alerted', 60.0))
        self.assertIsNone(recovered_at)
        self.assertIsNotNone(immobility_at)
        actor = conn.execute("SELECT actor FROM lifecycle_events WHERE lifecycle = 'recovered'").fetchone()
        self.assertEqual(actor, ('SafeStep detector',))

    def test_recovered_counts_as_closed_in_weekly_backlog(self):
        sql = weekly_report.BACKLOG_SQL
        self.assertEqual(sql.count("'cancelled','resolved','recovered'"), 3)


if __name__ == '__main__':
    unittest.main()
