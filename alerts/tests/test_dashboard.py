"""The web UI must never confuse a healthy API with active local detection."""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import alert_service as service


class MonitoringTests(unittest.TestCase):
    def request(self, heartbeats, key='test-key'):
        manager = service.IncidentManager()
        manager.heartbeats = heartbeats
        handler = object.__new__(service.Handler)
        handler.path = '/monitoring'
        handler.headers = {'X-API-Key': key}
        handler._send = Mock()
        with patch.object(service, 'CFG', {**service.DEFAULT_CONFIG, 'api_key': 'test-key'}), patch.object(service, 'MANAGER', manager):
            handler.do_GET()
        return handler._send.call_args.args

    def test_no_detector_is_waiting(self):
        self.assertEqual(self.request({}), (200, {'monitoring': 'waiting'}))

    def test_only_fresh_heartbeats_confirm_active_monitoring(self):
        fresh = {'t': time.time(), 'offline': False}
        self.assertEqual(self.request({'one': fresh}), (200, {'monitoring': 'active'}))
        stale = {'t': time.time() - 120, 'offline': False}
        self.assertEqual(self.request({'one': stale}), (200, {'monitoring': 'offline'}))
        self.assertEqual(self.request({'one': fresh, 'two': stale}), (200, {'monitoring': 'offline'}))

    def test_stopped_detector_is_offline(self):
        self.assertEqual(self.request({'one': {'t': time.time(), 'offline': True}}), (200, {'monitoring': 'offline'}))

    def test_monitoring_requires_existing_api_key(self):
        code, body = self.request({}, key='wrong')
        self.assertEqual(code, 401)
        self.assertIn('error', body)


if __name__ == '__main__':
    unittest.main()
