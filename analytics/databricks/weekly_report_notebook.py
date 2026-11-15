# Databricks notebook source
# No credentials or recipient details belong in this notebook.
import json
import sys
from pathlib import Path

dbutils.widgets.text('source_dir', '')
sys.path.insert(0, dbutils.widgets.get('source_dir'))
from databricks_report import run
from weekly_report import ReportError

for key, default in [('config_json', '{}'), ('namespace', 'workspace.safestep'),
                     ('smtp_host', 'smtp.gmail.com'), ('smtp_port', '587'), ('smtp_security', 'starttls'),
                     ('smtp_username', ''), ('mode', 'preview'), ('secret_scope', 'safestep-email'), ('retry_uncertain', 'false')]:
    dbutils.widgets.text(key, default)
try:
    result = run(spark, dbutils, json.loads(dbutils.widgets.get('config_json')),
                 dbutils.widgets.get('namespace'), mode=dbutils.widgets.get('mode'),
                 secret_scope=dbutils.widgets.get('secret_scope'),
                 retry_uncertain=dbutils.widgets.get('retry_uncertain') == 'true',
                 smtp_host=dbutils.widgets.get('smtp_host'), smtp_port=dbutils.widgets.get('smtp_port'),
                 smtp_security=dbutils.widgets.get('smtp_security'), smtp_username=dbutils.widgets.get('smtp_username'))
except Exception as exc:
    # Provider exceptions can include private recipient or authentication data.
    detail = str(exc) if isinstance(exc, ReportError) else type(exc).__name__
    raise RuntimeError('Weekly report failed: ' + detail + '. Check setup and delivery ledger.') from None
dbutils.notebook.exit(json.dumps(result))
