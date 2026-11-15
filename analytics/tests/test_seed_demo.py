"""Validate demo data through the real parser and incident view."""
from collections import Counter
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import seed_demo
from test_ingest import ingest


class DemoDataTests(unittest.TestCase):
    def test_replay_is_identical_and_events_cover_one_resident_and_four_weeks(self):
        end = date(2026, 11, 14)
        rows = seed_demo.generate(end)
        self.assertEqual(rows, seed_demo.generate(end))
        self.assertEqual({r['person'] for r in rows}, {seed_demo.PERSON})
        detected = [r for r in rows if r['lifecycle'] == 'detected']
        self.assertEqual(len(detected), 100)
        self.assertEqual(len({r['incident_id'] for r in detected}), 100)
        self.assertEqual(len({r['room'] for r in detected}), 5)
        self.assertEqual(len({r['type'] for r in detected}), 8)
        weekly = Counter()
        for row in detected:
            local = datetime.fromisoformat(row['detected_at']).astimezone(seed_demo.LOCAL)
            weekly[(local.date() - (end - timedelta(days=27))).days // 7] += 1
        self.assertEqual([weekly[i] for i in range(4)], [37, 28, 21, 14])
        self.assertTrue(all(datetime.fromisoformat(r['logged_at']) >=
                            datetime.fromisoformat(r['detected_at']) for r in rows))

    def test_generated_lifecycles_populate_dashboard_metrics(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.create_function('get_json_object', 2, lambda raw, path: json.loads(raw).get(path[2:]))
        conn.execute('CREATE TABLE lifecycle_events (' + ', '.join(
            name + ' ' + typ for name, typ in ingest.COLUMNS) + ')')
        schema = (Path(__file__).parents[1] / 'sql/schema.sql').read_text()
        view = schema[schema.index('CREATE OR REPLACE VIEW'):]
        conn.executescript(view.replace('CREATE OR REPLACE VIEW', 'CREATE VIEW').replace('safestep.', ''))
        rows = seed_demo.generate(date(2026, 11, 14))
        records = [ingest.row_to_record(json.dumps(row, sort_keys=True).encode()) for row in rows]
        self.assertEqual(len({r['row_id'] for r in records}), len(rows))
        conn.executemany('INSERT INTO lifecycle_events VALUES (' + ','.join('?' for _ in ingest.COLUMNS) + ')',
                         [tuple(r.values()) for r in records])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM incidents').fetchone()[0], 100)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM incidents WHERE event_type = 'fall_detected'").fetchone()[0], 40)
        outcomes = {r[0] for r in conn.execute('SELECT DISTINCT review_outcome FROM incidents')}
        self.assertEqual(outcomes, {None, 'confirmed_fall', 'no_fall', 'unsure'})
        for predicate in (
            'cancelled_at IS NOT NULL', 'escalations > 0', 'delivery_failed = 1',
            'delivery_failed = 1 AND alert_sent = 1', 'alerted_at IS NOT NULL AND alert_sent = 0',
            "event_type IN ('fall_detected', 'near_fall') AND status IN ('resolved', 'cancelled') AND review_outcome IS NULL",
        ):
            self.assertGreater(conn.execute('SELECT COUNT(*) FROM incidents WHERE ' + predicate).fetchone()[0], 0)
        acknowledged = conn.execute('SELECT alerted_at, acknowledged_at FROM incidents WHERE acknowledged_at IS NOT NULL')
        self.assertTrue(all(datetime.fromisoformat(ack) > datetime.fromisoformat(alert) for alert, ack in acknowledged))
        missing = conn.execute("SELECT COUNT(*) FROM incidents WHERE confidence IS NULL OR room IS NULL OR device_id IS NULL OR `timestamp` IS NULL").fetchone()[0]
        self.assertEqual(missing, 0)


if __name__ == '__main__':
    unittest.main()
