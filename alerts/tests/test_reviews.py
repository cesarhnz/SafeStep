import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import alert_service as service


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'events.jsonl'
        config = {**service.DEFAULT_CONFIG, 'event_log': str(self.path),
                  'ntfy_topic': 'test', 'ntfy_ack_topic': 'test-ack', 'api_key': 'test-key'}
        cfg = patch.object(service, 'CFG', config)
        cfg.start()
        self.addCleanup(cfg.stop)
        pub = patch.object(service, 'publish_async')
        self.publish = pub.start()
        self.addCleanup(pub.stop)
        self.manager = service.IncidentManager()
        self.inc = service.Incident({'type': 'fall_detected', 'room': 'Bedroom'}, service.CATALOG['fall_detected'])
        self.inc.status = 'alerted'
        self.manager.incidents[self.inc.id] = self.inc

    def rows(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_fall_name_aliases_keep_the_existing_fields_and_alert_policy(self):
        from contracts import to_internal
        for name in ('fall', 'possible_fall', 'fall_detected'):
            result = to_internal({'type': name, 'room': 'Bedroom', 'confidence': .9})
            self.assertEqual(result, {'type': 'fall_detected', 'room': 'Bedroom', 'confidence': .9})
        self.assertTrue(service.CATALOG['fall_detected']['confirm'])
        self.assertEqual(service.DEFAULT_CONFIG['confirm_window_s'], 30)

    def test_detector_contract_keeps_fields_for_each_fall_name(self):
        from contracts import to_internal
        payload = dict(event_id='source-1', device_id='cam-1', room='Bedroom',
                       timestamp='2026-11-15T12:00:00Z', confidence=.9,
                       person_detected=True, immobility_seconds=4)
        for name in ('fall', 'possible_fall', 'fall_detected'):
            result = to_internal({**payload, 'event_type': name})
            self.assertEqual(result['type'], 'fall_detected')
            for field, value in payload.items():
                self.assertEqual(result[field], value)

    def test_renamed_http_event_uses_existing_confirmation_and_deduplication(self):
        self.manager.incidents.clear()
        payload = dict(event_id='source-1', device_id='cam-1', room='Bedroom',
                       timestamp='2026-11-15T12:00:00Z', event_type='fall_detected',
                       confidence=.9, person_detected=True)
        with patch.object(service.threading, 'Timer') as timer, patch.object(self.manager, '_notify') as notify:
            code, response = self.request(payload, path='/events')
            self.assertEqual(code, 200)
            self.assertEqual(response['status'], 'pending_confirmation')
            incident = self.manager.incidents[response['incident_id']]
            self.assertEqual(incident.type, 'fall_detected')
            self.assertEqual(timer.call_args.args[0], 30)
            notify.assert_not_called()
            self.assertEqual(self.request(payload, path='/events')[1]['status'], 'duplicate')
            self.manager._confirm_timeout(incident.id)
            self.assertEqual(incident.status, 'alerted')
            notify.assert_called_once()
            self.manager.cancel(incident.id)
        self.assertEqual({row['type'] for row in self.rows()}, {'fall_detected'})

    def test_old_closed_incidents_still_accept_reviews_after_restart(self):
        for old_name in ('fall', 'possible_fall'):
            with self.subTest(old_name=old_name):
                self.path.write_text(json.dumps({'incident_id': 'OLD123', 'type': old_name,
                                                'status': 'resolved', 'room': 'Bedroom'}) + '\n')
                restarted = service.IncidentManager()
                result = restarted.review('OLD123', 'unsure')
                self.assertEqual(result['type'], 'fall_detected')
                self.assertEqual(result['review_outcome'], 'unsure')
                self.assertEqual(result['status'], 'resolved')

    def test_cancel_does_not_label_no_fall_and_offers_three_buttons(self):
        self.manager.cancel(self.inc.id)
        self.assertIsNone(self.inc.review_outcome)
        args, kwargs = self.publish.call_args
        self.assertEqual(len(kwargs['actions']), 3)
        self.assertEqual([a['body'] for a in kwargs['actions']],
                         [f'review {self.inc.id} {v}' for v in ('confirmed_fall', 'no_fall', 'unsure')])
        self.assertTrue(all(a['url'].endswith('/test-ack') for a in kwargs['actions']))

    def test_review_corrections_keep_history_and_do_not_change_status(self):
        self.manager.cancel(self.inc.id)
        self.manager.review(self.inc.id, 'unsure', by='Sam')
        self.manager.review(self.inc.id, 'no_fall', ' Sat down deliberately ', by='Sam')
        self.manager.review(self.inc.id, 'no_fall', 'Sat down deliberately', by='Sam')
        reviews = [r for r in self.rows() if r['lifecycle'] == 'reviewed']
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[-1]['review_note'], 'Sat down deliberately')
        self.assertEqual(reviews[-1]['reviewed_by'], 'Sam')
        self.assertEqual(self.inc.status, 'cancelled')

    def test_active_incident_cannot_be_reviewed_or_silenced(self):
        timer = Mock()
        self.inc.timer = timer
        with self.assertRaises(ValueError):
            self.manager.review(self.inc.id, 'no_fall')
        timer.cancel.assert_not_called()
        self.assertEqual(self.inc.status, 'alerted')

    def test_invalid_review_is_rejected_without_mutation(self):
        self.manager.resolve(self.inc.id)
        for outcome, note, actor in [('false_alarm', None, 'Sam'), ('unsure', 'x' * 2001, 'Sam'),
                                     ('unsure', 123, 'Sam'), ('unsure', None, '')]:
            with self.subTest(outcome=outcome, actor=actor), self.assertRaises(ValueError):
                self.manager.review(self.inc.id, outcome, note, actor)
        self.assertIsNone(self.inc.review_outcome)
        with self.assertRaises(KeyError):
            self.manager.review('missing', 'unsure')

    def test_review_recovers_after_restart_and_allows_correction(self):
        self.manager.resolve(self.inc.id)
        self.manager.review(self.inc.id, 'unsure', by='Sam')
        restarted = service.IncidentManager()
        result = restarted.review(self.inc.id.lower(), 'confirmed_fall', 'Witnessed fall', by='Alex')
        self.assertEqual(result['review_outcome'], 'confirmed_fall')
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['reviewed_by'], 'Alex')

    def test_near_fall_supported_but_device_fault_not_reviewable(self):
        self.inc.type = 'near_fall'
        self.manager.resolve(self.inc.id)
        self.manager.review(self.inc.id, 'no_fall')
        self.inc.type = 'camera_offline'
        with self.assertRaises(ValueError):
            self.manager.review(self.inc.id, 'unsure')

    def test_failed_save_can_be_retried_without_false_receipt(self):
        self.manager.resolve(self.inc.id)
        self.publish.reset_mock()
        with patch.object(Path, 'open', side_effect=PermissionError('read only')):
            with self.assertRaises(OSError):
                self.manager.review(self.inc.id, 'no_fall')
        self.assertIsNone(self.inc.review_outcome)
        self.publish.assert_not_called()
        self.manager.review(self.inc.id, 'no_fall')
        self.assertEqual(self.rows()[-1]['lifecycle'], 'reviewed')

    def test_ntfy_button_command_accepts_notes_and_invalid_command_does_not_stop_listener(self):
        self.manager.resolve(self.inc.id)
        button = self.publish.call_args.kwargs['actions'][0]
        response = Mock()
        response.__enter__ = Mock(return_value=[
            json.dumps({'id': '1', 'event': 'message',
                        'message': f'review {self.inc.id} invalid'}).encode(),
            json.dumps({'id': '2', 'event': 'message',
                        'message': button['body'] + ' Witnessed near bed'}).encode(),
        ])
        response.__exit__ = Mock(return_value=False)
        with patch.object(service, 'MANAGER', self.manager), patch.object(
                service.urllib.request, 'urlopen', side_effect=[response, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                service.ack_listener_loop()
        self.assertEqual(self.inc.review_outcome, 'confirmed_fall')
        self.assertEqual(self.inc.review_note, 'Witnessed near bed')
        self.assertEqual(self.inc.reviewed_by, 'Caregiver (ntfy)')

    def request(self, body, key='test-key', path=None):
        handler = service.Handler.__new__(service.Handler)
        data = json.dumps(body).encode()
        handler.headers = {'Content-Length': str(len(data)), 'X-API-Key': key}
        handler.rfile = io.BytesIO(data)
        handler.path = path or f'/incidents/{self.inc.id}/review'
        handler._send = Mock()
        with patch.object(service, 'MANAGER', self.manager):
            handler.do_POST()
        return handler._send.call_args.args

    def test_http_review_requires_auth_and_validates_payload(self):
        self.manager.resolve(self.inc.id)
        self.assertEqual(self.request({'outcome': 'no_fall'}, key='wrong')[0], 401)
        self.assertEqual(self.request({'outcome': 'invalid'})[0], 400)
        self.assertEqual(self.request({})[0], 400)
        code, result = self.request({'outcome': 'confirmed_fall', 'note': 'Witnessed', 'by': 'Sam'})
        self.assertEqual(code, 200)
        self.assertEqual(result['review_note'], 'Witnessed')


if __name__ == '__main__':
    unittest.main()
