import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from analytics import weekly_report as report, databricks_report as hosted
from analytics.deploy_report_job import job_settings


class HostedReportTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 15, 15, tzinfo=timezone.utc)
        self.config = report.validate_config(dict(sender='sender@example.com', recipients=['a@example.com', 'b@example.com'], dashboard_url='https://example.com/dashboard'))
        self.start, self.end = report.reporting_window(self.now, self.config)
        self.rendered = report.render_report(report.demo_report(self.now), self.start, self.end, self.now, self.config)

    def test_job_serializes_writers_and_defaults_to_paused_preview(self):
        settings = job_settings('/Users/test/reports', self.config, 'workspace.safestep')
        self.assertEqual(settings['max_concurrent_runs'], 1)
        self.assertEqual(settings['schedule']['quartz_cron_expression'], '0 0 8 ? * SUN')
        custom = {**self.config, 'weekday': 'friday', 'time': '17:45', 'timezone': 'Europe/London'}
        self.assertEqual(job_settings('/x', custom, 'workspace.safestep')['schedule']['quartz_cron_expression'], '0 45 17 ? * FRI')
        self.assertEqual(settings['schedule']['pause_status'], 'PAUSED')
        self.assertEqual(settings['tasks'][0]['max_retries'], 0)
        params = settings['tasks'][0]['notebook_task']['base_parameters']
        self.assertEqual(params['mode'], 'preview')
        self.assertNotIn('password', params)
        self.assertEqual(job_settings('/Users/test/reports', self.config, 'workspace.safestep', True)['schedule']['pause_status'], 'UNPAUSED')

    def test_preview_never_requests_secrets_or_writes_ledger(self):
        dbutils = MagicMock()
        with patch.object(report, 'fetch_report', return_value=report.demo_report(self.now)), patch.object(hosted, 'DeltaLedger') as ledger:
            result = hosted.run(MagicMock(), dbutils, self.config, 'workspace.safestep')
        self.assertGreater(result['html_bytes'], 1000)
        dbutils.secrets.get.assert_not_called()
        ledger.assert_not_called()

    def test_delivery_partial_failure_preserves_other_recipients(self):
        ledger = MagicMock()
        ledger.claim.side_effect = ['one', 'two']
        send = MagicMock(side_effect=[TimeoutError('private'), None])
        with self.assertRaises(report.ReportError):
            hosted.deliver(self.rendered, self.start, self.end, self.config, ledger, self.now, send)
        self.assertEqual(send.call_count, 2)
        ledger.mark_sent.assert_called_once()
        self.assertEqual(ledger.mark_sent.call_args.args[1], 'two')
        for call in send.call_args_list:
            self.assertEqual(call.args[0]['To'], call.args[1])

    def test_sent_and_pending_claims_never_send(self):
        ledger = MagicMock()
        ledger.claim.side_effect = [None, report.ReportError('uncertain')]
        send = MagicMock()
        with self.assertRaises(report.ReportError):
            hosted.deliver(self.rendered, self.start, self.end, self.config, ledger, self.now, send)
        send.assert_not_called()

    def test_delta_claim_is_parameterized_and_fails_on_duplicate_keys(self):
        spark = MagicMock()
        ledger = hosted.DeltaLedger(spark, 'workspace.safestep.ledger')
        with patch.object(ledger, 'execute', side_effect=[[], [('pending', 'claim')]]), patch.object(hosted.uuid, 'uuid4') as uid:
            uid.return_value.hex = 'claim'
            self.assertEqual(ledger.claim("unsafe'key"), 'claim')
            sql, bindings = ledger.execute.call_args_list[0].args
            self.assertNotIn("unsafe'key", sql)
            self.assertEqual(bindings['key'], "unsafe'key")
        with patch.object(ledger, 'execute', side_effect=[[], [('sent', 'x'), ('sent', 'y')]]):
            with self.assertRaises(report.ReportError):
                ledger.claim('duplicate')
        with self.assertRaises(report.ReportError):
            hosted.DeltaLedger(spark, 'bad; DROP TABLE x')

    def test_send_restores_secret_environment_on_failure(self):
        dbutils = MagicMock()
        dbutils.secrets.get.return_value = 'app-password'
        with patch.dict(os.environ, {'SMTP_PASSWORD': 'old-value'}), patch.object(report, 'fetch_report', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                hosted.run(MagicMock(), dbutils, self.config, 'workspace.safestep', mode='send')
            self.assertEqual(os.environ['SMTP_PASSWORD'], 'old-value')
        dbutils.secrets.get.assert_called_once_with(scope='safestep-email', key='smtp-password')

    def test_connectivity_probe_never_authenticates_or_sends(self):
        with patch.object(hosted.smtplib, 'SMTP') as factory:
            result = hosted.check_smtp_connectivity('smtp.gmail.com', 587, 'starttls')
            smtp = factory.return_value.__enter__.return_value
            smtp.starttls.assert_called_once()
            smtp.login.assert_not_called()
            smtp.send_message.assert_not_called()
            self.assertIn('succeeded', result)
        with patch.object(hosted.smtplib, 'SMTP', side_effect=TimeoutError):
            self.assertIn('FAILED', hosted.check_smtp_connectivity('smtp.gmail.com', 587, 'starttls'))

    def test_hosted_queries_use_spark_adapter_and_namespace(self):
        spark = MagicMock()
        cursor = hosted.SparkCursor(spark)
        spark.sql.return_value.columns = ['count']
        spark.sql.return_value.collect.return_value = [(7,)]
        cursor.execute('SELECT :value', dict(value=7))
        spark.sql.assert_called_once_with('SELECT :value', args=dict(value=7))
        self.assertEqual(cursor.fetchall(), [(7,)])
        self.assertEqual(cursor.description, [('count',)])
        with self.assertRaises(report.ReportError):
            report.fetch_report(self.start, self.end, connect=lambda: None, namespace='bad;sql')


if __name__ == '__main__':
    unittest.main()
