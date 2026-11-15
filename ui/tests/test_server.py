import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ui_server', Path(__file__).parents[1] / 'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class DashboardTests(unittest.TestCase):
    def event(self, **extra):
        return dict(incident_id='AB1234', type='fall_detected', status='alerted', room='Living room',
                    detected_at='2026-11-15T10:00:00Z', **extra)

    def test_delivery_requires_explicit_transport_evidence(self):
        for marker, expected in [(True, 'not_sent'), (False, 'accepted'), (None, 'unknown')]:
            event = self.event(lifecycle='delivered')
            if marker is not None:
                event['dry_run'] = marker
            result = server.normalize_events([event], [])
            self.assertEqual(result[0]['notification'], expected)

    def test_dry_run_never_claims_notifications_sent(self):
        event = self.event(lifecycle='delivered', dry_run=True)
        self.assertEqual(server.normalize_events([event], [])[0]['notification'], 'not_sent')

    def test_delivery_failure_survives_incident_merge(self):
        event = self.event(lifecycle='delivery_failed')
        current = {**self.event(), 'status': 'resolved'}
        result = server.normalize_events([event], [current])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['notification'], 'failed')
        self.assertEqual(result[0]['status'], 'resolved')
        self.assertEqual(result[0]['source'], 'real')
        self.assertNotIn('api_key', result[0])

    def test_old_unresolved_fall_is_not_hidden_by_recent_routine_activity(self):
        fall = self.event(lifecycle='detected')
        rows = [fall] + [dict(incident_id=f'HAZARD{i}', type='near_fall', status='resolved',
                              room='Hallway', detected_at='2026-11-16T10:00:00Z') for i in range(110)]
        events = server.normalize_events(rows, [])
        self.assertEqual(len(events), 100)
        self.assertIn('AB1234', {event['id'] for event in events})

    def test_log_tolerates_partial_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            path.write_text(json.dumps(self.event()) + '\n{"incomplete":')
            rows, available = server.log_records(path)
            self.assertTrue(available)
            self.assertEqual(len(rows), 1)
            self.assertEqual(server.log_records(path.with_name('missing')), ([], False))

    def test_dashboard_uses_only_configured_direct_url(self):
        with patch.dict('os.environ', {'SAFESTEP_DASHBOARD_URL': 'https://dbc-example.cloud.databricks.com/dashboardsv3/123/published'}):
            self.assertIn('/dashboardsv3/123/published', server.dashboard_url())
        with patch.dict('os.environ', {'SAFESTEP_DASHBOARD_URL': 'https://databricks.com.attacker.net/dashboards/123'}):
            self.assertIsNone(server.dashboard_url())

    def test_offline_snapshot_preserves_history_without_claiming_monitoring(self):
        with patch.object(server.urllib.request, 'urlopen', side_effect=OSError), patch.object(server, 'read_json', return_value={}), patch.object(server, 'log_records', return_value=([self.event(lifecycle='detected')], True)):
            data = server.snapshot()
        self.assertFalse(data['connected'])
        self.assertEqual(data['monitoring'], 'unknown')
        self.assertEqual(len(data['events']), 1)

class ProvenanceTests(unittest.TestCase):
    def test_existing_simulator_records_remain_demo_through_lifecycle_updates(self):
        base = dict(incident_id='DEMO1', type='fall_detected', status='alerted', room='Living room',
                    detected_at='2026-11-15T10:00:00Z')
        rows = [{**base, 'lifecycle': 'detected', 'raw_event': {'camera_id': 'demo-cam'}},
                {**base, 'lifecycle': 'alerted'}]
        result = server.normalize_events(rows, [])
        self.assertEqual(result[0]['source'], 'demo')
        self.assertEqual(result[0]['notification'], 'unknown')


if __name__ == '__main__':
    unittest.main()
