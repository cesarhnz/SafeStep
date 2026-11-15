"""Exercise review ingestion and incident-view joins with SQLite's SQL window support."""
import json
import sqlite3
import unittest
from pathlib import Path
from test_ingest import ingest


class ReviewAnalyticsTests(unittest.TestCase):
    def test_historical_fall_names_are_exposed_under_the_current_name(self):
        for name in ('fall', 'possible_fall', 'fall_detected'):
            result = ingest.row_to_record(json.dumps({'type': name, 'room': 'Bedroom'}).encode())
            self.assertEqual(result['event_type'], 'fall_detected')
            self.assertEqual(result['room'], 'Bedroom')

    def test_incident_view_normalizes_preexisting_rows_without_reingestion(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.create_function('get_json_object', 2,
                             lambda raw, path: json.loads(raw).get(path[2:]))
        conn.execute('CREATE TABLE lifecycle_events (' + ', '.join(
            name + ' ' + typ for name, typ in ingest.COLUMNS) + ')')
        schema = (Path(__file__).parents[1] / 'sql/schema.sql').read_text()
        view = schema[schema.index('CREATE OR REPLACE VIEW'):]
        conn.executescript(view.replace('CREATE OR REPLACE VIEW', 'CREATE VIEW').replace('safestep.', ''))
        names = ('fall', 'possible_fall', 'fall_detected', 'near_fall', 'camera_offline')
        for index, name in enumerate(names):
            # Insert the stored v1 name directly, bypassing the ingestion normalizer.
            conn.execute("INSERT INTO lifecycle_events (row_id, incident_id, event_type, status, room, raw) VALUES (?, ?, ?, 'resolved', 'Bedroom', '{}')",
                         (str(index), str(index), name))
        rows = conn.execute('SELECT event_type, room FROM incidents ORDER BY incident_id').fetchall()
        self.assertEqual(rows, [('fall_detected', 'Bedroom')] * 3 +
                               [('near_fall', 'Bedroom'), ('camera_offline', 'Bedroom')])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM incidents WHERE event_type = 'fall_detected'").fetchone()[0], 3)

    def test_latest_review_survives_later_nonreview_rows_and_replay(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.create_function('get_json_object', 2,
                             lambda raw, path: json.loads(raw).get(path[2:]))
        conn.execute('CREATE TABLE lifecycle_events (' + ', '.join(
            name + ' ' + typ for name, typ in ingest.COLUMNS) + ')')
        schema = (Path(__file__).parents[1] / 'sql/schema.sql').read_text()
        view = schema[schema.index('CREATE OR REPLACE VIEW'):]
        conn.executescript(view.replace('CREATE OR REPLACE VIEW', 'CREATE VIEW').replace('safestep.', ''))
        rows = [
            {'lifecycle': 'cancelled', 'logged_at': '2026-11-14T20:00:00Z'},
            {'lifecycle': 'reviewed', 'logged_at': '2026-11-14T20:01:00Z',
             'review_outcome': 'unsure', 'reviewed_by': 'Sam'},
            {'lifecycle': 'reviewed', 'logged_at': '2026-11-14T20:02:00Z',
             'review_outcome': 'no_fall', 'review_note': 'Sat down', 'reviewed_by': 'Alex'},
            {'lifecycle': 'delivered', 'logged_at': '2026-11-14T20:03:00Z'},
        ]
        for row in rows:
            record = ingest.row_to_record(json.dumps({
                'incident_id': 'A1', 'type': 'fall_detected', 'status': 'cancelled', **row}).encode())
            if row['lifecycle'] == 'reviewed':
                self.assertEqual(json.loads(record['raw'])['review_outcome'], row['review_outcome'])
            # Replayed MERGE batches deduplicate these identical content hashes.
            sql, params = ingest.build_merge([record, record])
            self.assertEqual(len(params), len(ingest.COLUMNS))
            conn.execute('INSERT INTO lifecycle_events VALUES (' + ','.join('?' for _ in record) + ')',
                         tuple(record.values()))
        result = conn.execute('SELECT status, review_outcome, review_note, reviewed_by FROM incidents').fetchall()
        self.assertEqual(result, [('cancelled', 'no_fall', 'Sat down', 'Alex')])
        conn.execute("DELETE FROM lifecycle_events WHERE lifecycle = 'reviewed'")
        self.assertEqual(conn.execute('SELECT review_outcome FROM incidents').fetchone(), (None,))


if __name__ == '__main__':
    unittest.main()
