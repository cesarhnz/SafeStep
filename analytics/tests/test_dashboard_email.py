import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analytics import deploy_dashboard_email as native
from analytics.weekly_report import validate_config, ReportError


class DashboardEmailTests(unittest.TestCase):
    def setUp(self):
        self.config = validate_config(dict(recipients=['caregiver@example.com'], sender='', time='08:00', weekday='sunday'))
        self.dashboard_id = 'a' * 32

    def test_native_email_needs_only_recipient_and_has_no_smtp_task(self):
        settings = native.snapshot_settings(self.config, self.dashboard_id, 'warehouse')
        self.assertEqual(settings['schedule'], dict(quartz_cron_expression='0 0 8 ? * SUN', timezone_id='America/Chicago', pause_status='UNPAUSED'))
        task = settings['tasks'][0]
        self.assertNotIn('notebook_task', task)
        self.assertEqual(task['dashboard_task']['subscription']['subscribers'], [dict(user_name='caregiver@example.com')])
        self.assertFalse(task['dashboard_task']['subscription']['paused'])
        self.assertNotIn('SMTP', json.dumps(settings))
        self.assertNotIn('secret', json.dumps(settings))

    def test_native_schedule_is_customizable(self):
        config = {**self.config, 'weekday': 'friday', 'time': '17:45', 'timezone': 'Europe/London'}
        settings = native.snapshot_settings(config, self.dashboard_id, 'warehouse')
        self.assertEqual(settings['schedule']['quartz_cron_expression'], '0 45 17 ? * FRI')
        self.assertEqual(settings['schedule']['timezone_id'], 'Europe/London')
        with self.assertRaises(ValueError):
            native.snapshot_settings({**config, 'recipients': []}, self.dashboard_id, 'warehouse')
        with self.assertRaises(ReportError):
            native.snapshot_settings({**config, 'time': '25:00'}, self.dashboard_id, 'warehouse')

    def test_existing_custom_job_is_replaced_and_no_email_is_sent_unless_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'databricks-deployment.json').write_text(json.dumps(dict(job_id=123)))
            state = root / 'dashboard-email-deployment.json'
            calls = []

            def api(method, endpoint, data=None):
                calls.append((method, endpoint, data))
                if endpoint.endswith('/get?job_id=123'):
                    return dict(settings=dict(name=native.OLD_NAME))
                if endpoint.endswith('/jobs/run-now'):
                    return dict(run_id=456)
                if method == 'GET' and endpoint.endswith(self.dashboard_id):
                    return dict(warehouse_id='warehouse')
                return {}

            with patch.object(native, 'api', side_effect=api):
                native.deploy(self.config, self.dashboard_id, state)
            reset = next(c for c in calls if c[1].endswith('/jobs/reset'))
            self.assertEqual(reset[2]['job_id'], 123)
            self.assertEqual(reset[2]['new_settings']['name'], native.NAME)
            self.assertFalse(any(c[1].endswith('/jobs/run-now') for c in calls))
            calls.clear()
            with patch.object(native, 'api', side_effect=api):
                result = native.deploy(self.config, self.dashboard_id, state, run_now=True)
            self.assertEqual(result['run_id'], 456)
            self.assertEqual(sum(c[1].endswith('/jobs/run-now') for c in calls), 1)
            self.assertEqual(json.loads(state.read_text())['transport'], 'databricks_snapshot')


if __name__ == '__main__':
    unittest.main()
