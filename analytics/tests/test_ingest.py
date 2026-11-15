import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location('ingest', Path(__file__).parents[1] / 'ingest.py')
ingest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingest)


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / 'events.jsonl'
        self.checkpoint = Path(self.temp.name) / 'nested' / 'offset'
        self.log.write_bytes(b'')
        self.patch = patch.multiple(ingest, LOG_PATH=self.log, CHECKPOINT=self.checkpoint)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.connection = MagicMock()
        self.connect = MagicMock(return_value=self.connection)

    def row(self, i=0):
        return json.dumps({'incident_id': str(i), 'logged_at': '2026-11-14T20:00:00Z',
                           'lifecycle': 'detected', 'type': 'fall_detected'}).encode() + b'\n'

    def test_resume_and_partial_line(self):
        self.log.write_bytes(self.row() + self.row(1)[:-1])
        self.assertEqual(ingest.run_once(self.connect), 1)
        self.assertEqual(ingest.run_once(self.connect), 0)
        with self.log.open('ab') as f:
            f.write(b'\n')
        self.assertEqual(ingest.run_once(self.connect), 1)

    def test_failed_batch_does_not_advance(self):
        self.log.write_bytes(self.row())
        self.connection.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            ingest.run_once(self.connect)
        self.assertFalse(self.checkpoint.exists())

    def test_replaced_log_replays_even_when_larger(self):
        self.log.write_bytes(self.row())
        ingest.run_once(self.connect)
        replacement = self.log.with_suffix('.new')
        replacement.write_bytes(self.row(1) + self.row(2))
        replacement.replace(self.log)
        self.assertEqual(ingest.run_once(self.connect), 2)

    def test_dry_run_never_connects_or_checkpoints(self):
        self.log.write_bytes(self.row())
        with patch('builtins.print'):
            self.assertEqual(ingest.run_once(self.connect, dry_run=True), 1)
        self.connect.assert_not_called()
        self.assertFalse(self.checkpoint.exists())

    def test_bad_row_stops_without_checkpoint(self):
        self.log.write_bytes(b'broken\n')
        with self.assertRaises(ValueError):
            ingest.run_once(self.connect)
        self.assertFalse(self.checkpoint.exists())

    def test_parameter_limit_and_duplicate_rows(self):
        self.log.write_bytes(b''.join(self.row(i) for i in range(25)))
        self.assertEqual(ingest.run_once(self.connect), 25)
        calls = self.connection.cursor.return_value.__enter__.return_value.execute.call_args_list
        self.assertTrue(all(len(call.args[1]) <= 255 for call in calls))
        record = ingest.row_to_record(self.row())
        _, params = ingest.build_merge([record, record])
        self.assertEqual(len(params), len(ingest.COLUMNS))

    def test_init_uses_configured_namespace(self):
        with patch.object(ingest, 'TABLE', 'demo.safety.lifecycle_events'):
            ingest.initialize(self.connect)
        calls = self.connection.cursor.return_value.__enter__.return_value.execute.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertIn('CREATE SCHEMA IF NOT EXISTS demo.safety', calls[0].args[0])
        self.assertIn('USING DELTA', calls[1].args[0])
        self.assertIn('demo.safety.incidents', calls[2].args[0])


if __name__ == '__main__':
    unittest.main()
