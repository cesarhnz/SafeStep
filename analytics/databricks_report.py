"""Databricks job runtime: Spark reads, durable Delta delivery intents, secret-backed SMTP."""
import hashlib
import json
import os
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timezone

try:
    from . import weekly_report as report
except ImportError:
    import weekly_report as report


class SparkConnection:
    """Small DB-API adapter so local and hosted reports execute identical SQL."""
    def __init__(self, spark):
        self.spark = spark

    def cursor(self):
        return SparkCursor(self.spark)

    def close(self):
        pass  # Spark belongs to the job runtime.


class SparkCursor:
    def __init__(self, spark):
        self.spark = spark

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, parameters=None):
        self.frame = self.spark.sql(sql, args=parameters or {})
        self.rows = self.frame.collect()  # Force DDL/DML completion before SMTP.
        self.description = [(name,) for name in self.frame.columns]

    def fetchall(self):
        return self.rows


class DeltaLedger:
    """Use one owning Job with max_concurrent_runs=1 per ledger.

    Delta does not enforce unique keys. Do not run independent writer jobs against
    this ledger. Pending intents never expire or auto-retry after an SMTP ambiguity.
    """
    def __init__(self, spark, table):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){2}', table):
            raise report.ReportError('Ledger needs a catalog.schema.table identifier')
        self.spark, self.table = spark, table
        self.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
            delivery_key STRING, status STRING, claim_token STRING, updated_at TIMESTAMP
        ) USING DELTA TBLPROPERTIES ('delta.isolationLevel' = 'Serializable')''')

    def execute(self, sql, args=None):
        return self.spark.sql(sql, args=args or {}).collect()

    def claim(self, key, retry_uncertain=False):
        token = uuid.uuid4().hex
        self.execute(f'''MERGE INTO {self.table} t
            USING (SELECT :key AS delivery_key, :token AS claim_token) s
            ON t.delivery_key = s.delivery_key
            WHEN MATCHED AND t.status = 'pending' AND :retry THEN UPDATE SET
              claim_token = s.claim_token, updated_at = current_timestamp()
            WHEN NOT MATCHED THEN INSERT (delivery_key, status, claim_token, updated_at) VALUES
              (s.delivery_key, 'pending', s.claim_token, current_timestamp())''',
                     dict(key=key, token=token, retry=retry_uncertain))
        rows = self.execute(f'SELECT status, claim_token FROM {self.table} WHERE delivery_key = :key', dict(key=key))
        if len(rows) != 1:
            raise report.ReportError('Invalid delivery ledger: expected exactly one row; stop other writers')
        status, owner = rows[0]
        if status == 'sent':
            return None
        if owner != token:
            raise report.ReportError('Delivery outcome is uncertain; inspect provider logs before an explicit retry')
        return token

    def mark_sent(self, key, token):
        self.execute(f'''UPDATE {self.table} SET status = 'sent', updated_at = current_timestamp()
            WHERE delivery_key = :key AND claim_token = :token AND status = 'pending' ''', dict(key=key, token=token))


def deliver(rendered, start, end, config, ledger, now, send=report.smtp_send, retry_uncertain=False):
    report.validate_delivery(config)
    failures, sent = [], 0
    for recipient in config['recipients']:
        key = hashlib.sha256(json.dumps([config['name'], config['timezone'], start.isoformat(), end.isoformat(), recipient]).encode()).hexdigest()
        stage = 'ledger claim'
        try:
            token = ledger.claim(key, retry_uncertain)
            if token is None:
                continue
            stage = 'email delivery'
            send(report.build_message(rendered, config, recipient, key, now), recipient)
            stage = 'ledger completion'
            ledger.mark_sent(key, token)
            sent += 1
        except Exception as exc:
            detail = str(exc) if isinstance(exc, report.ReportError) else type(exc).__name__
            failures.append(stage + ': ' + detail)
    if failures:
        raise report.ReportError(f'{sent} sent; {len(failures)} failed or uncertain ({failures[0]}). Inspect job permissions and provider logs before retrying.')
    return sent


def run(spark, dbutils, config, namespace, mode='preview', secret_scope='safestep-email', retry_uncertain=False, smtp_host='smtp.gmail.com',
        smtp_port='587', smtp_security='starttls', smtp_username=None):
    config = report.validate_config(config)
    if mode not in ('preview', 'verify', 'send'):
        raise report.ReportError('mode must be preview, verify, or send')
    now = datetime.now(timezone.utc)
    start, end = report.reporting_window(now, config)
    if mode == 'send':
        report.validate_delivery(config)
        # All transport settings are explicit; never inherit stale worker environment.
        settings = dict(SMTP_HOST=smtp_host, SMTP_PORT=str(smtp_port), SMTP_SECURITY=smtp_security,
                        SMTP_USERNAME=smtp_username or config['sender'])
        settings['SMTP_PASSWORD'] = dbutils.secrets.get(scope=secret_scope, key='smtp-password')
        # Environment restored even when queries/SMTP fail; no secrets in job parameters.
        previous = {key: os.environ.get(key) for key in settings}
        try:
            os.environ.update(settings)
            report.smtp_settings()
            data = report.fetch_report(start, end, connect=lambda: SparkConnection(spark), namespace=namespace)
            ledger = DeltaLedger(spark, namespace + '.weekly_report_deliveries')
            return deliver(report.render_report(data, start, end, now, config), start, end,
                           config, ledger, now, retry_uncertain=retry_uncertain)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    data = report.fetch_report(start, end, connect=lambda: SparkConnection(spark), namespace=namespace)
    # Preview validates live queries and rendering without SMTP or ledger writes.
    rendered = report.render_report(data, start, end, now, config)
    result = dict(mode=mode, html_bytes=len(rendered[2].encode()), periods=2)
    if mode == 'verify':
        result['delivery_checks'] = verify_ledger(spark, namespace, config, start, end, rendered, now)
        result['smtp_connectivity'] = check_smtp_connectivity(smtp_host, smtp_port, smtp_security)
    return result


def verify_ledger(spark, namespace, config, start, end, rendered, now):
    """Real Delta writes in a disposable table, with SMTP replaced by an in-memory sink."""
    table = namespace + '.weekly_report_test_' + uuid.uuid4().hex
    ledger = DeltaLedger(spark, table)
    config = {**config, 'sender': 'test@example.com', 'recipients': ['test@example.com'],
              'dashboard_url': 'https://example.com/test'}
    messages = []
    send = lambda message, recipient: messages.append(message)
    try:
        assert deliver(rendered, start, end, config, ledger, now, send=send) == 1
        assert deliver(rendered, start, end, config, ledger, now, send=send) == 0
        assert len(messages) == 1
        assert [p.get_content_type() for p in messages[0].iter_parts()] == ['text/plain', 'text/html']
        token = ledger.claim('ambiguous-test')
        try:
            ledger.claim('ambiguous-test')
        except report.ReportError:
            pass
        else:
            raise AssertionError('Pending delivery was automatically retried')
        retry_token = ledger.claim('ambiguous-test', retry_uncertain=True)
        assert retry_token != token
        ledger.mark_sent('ambiguous-test', retry_token)
        assert ledger.claim('ambiguous-test') is None
        return ['Delta persistence', 'duplicate suppression', 'pending protection', 'explicit retry', 'multipart email']
    finally:
        ledger.execute('DROP TABLE ' + table)


def check_smtp_connectivity(host, port, security):
    """TLS handshake only. No authentication or email is sent."""
    if security not in ('starttls', 'ssl'):
        return 'FAILED: invalid SMTP security mode'
    try:
        factory = smtplib.SMTP_SSL if security == 'ssl' else smtplib.SMTP
        kwargs = dict(host=host, port=int(port), timeout=10)
        if security == 'ssl':
            kwargs['context'] = ssl.create_default_context()
        with factory(**kwargs) as smtp:
            smtp.ehlo()
            if security == 'starttls':
                smtp.starttls(context=ssl.create_default_context())
        return 'TLS connection succeeded; authentication and inbox delivery still untested'
    except Exception as exc:
        return 'FAILED: ' + type(exc).__name__ + '; check outbound network policy and SMTP endpoint'
