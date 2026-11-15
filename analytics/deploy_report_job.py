#!/usr/bin/env python3
"""Upload the report and create/update its Databricks Job. Defaults to paused preview mode."""
import argparse
import base64
import json
import os
from pathlib import Path
import ssl
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent


def api(method, endpoint, data=None):
    import certifi
    host = os.environ.get('DATABRICKS_SERVER_HOSTNAME') or os.environ.get('DATABRICKS_HOST', '')
    host = host.rstrip('/')
    if '://' not in host:
        host = 'https://' + host
    if urlparse(host).scheme != 'https' or not os.environ.get('DATABRICKS_TOKEN'):
        raise ValueError('Export Databricks hostname and token')
    request = Request(host + endpoint, data=json.dumps(data).encode() if data is not None else None,
                      headers={'Authorization': 'Bearer ' + os.environ['DATABRICKS_TOKEN'],
                               'Content-Type': 'application/json'}, method=method)
    try:
        with urlopen(request, context=ssl.create_default_context(cafile=os.getenv('DATABRICKS_CA_BUNDLE') or certifi.where()), timeout=60) as response:
            return json.load(response)
    except HTTPError as exc:
        # Only the API error code, never echo request bodies/secrets.
        try:
            code = json.loads(exc.read()).get('error_code', 'HTTP_ERROR')
        except ValueError:
            code = 'HTTP_ERROR'
        raise RuntimeError(f'Databricks API {exc.code} ({code}) at {endpoint.split("?")[0]}') from None


def job_settings(folder, config, namespace, active=False):
    return dict(name='SafeStep Weekly Caregiver Report', max_concurrent_runs=1,
                schedule=dict(quartz_cron_expression=f'0 {int(config["time"].split(":")[1])} {int(config["time"].split(":")[0])} ? * {config["weekday"][:3].upper()}', timezone_id=config['timezone'],
                              pause_status='UNPAUSED' if active else 'PAUSED'),
                tasks=[dict(task_key='weekly_digest', timeout_seconds=1200, max_retries=0,
                            notebook_task=dict(notebook_path=folder + '/run_weekly_digest', source='WORKSPACE',
                                               base_parameters=dict(source_dir='/Workspace' + folder,
                                                                    config_json=json.dumps(config), namespace=namespace,
                                                                    mode='send' if active else 'preview',
                                                                    secret_scope='safestep-email', retry_uncertain='false',
                                                                    smtp_host=os.getenv('SMTP_HOST', 'smtp.gmail.com'),
                                                                    smtp_port=os.getenv('SMTP_PORT', '587'),
                                                                    smtp_security=os.getenv('SMTP_SECURITY', 'starttls'),
                                                                    smtp_username=os.getenv('SMTP_USERNAME', config['sender']))))])


def deploy(config, namespace, state, active=False):
    try:
        from .weekly_report import validate_config, validate_delivery
    except ImportError:
        from weekly_report import validate_config, validate_delivery
    config = validate_config(config)
    if active:
        validate_delivery(config)
        keys = api('GET', '/api/2.0/secrets/list?scope=safestep-email').get('secrets', [])
        if not any(key['key'] == 'smtp-password' for key in keys):
            raise ValueError('Store smtp-password in the safestep-email Databricks secret scope before activation')
    user = api('GET', '/api/2.0/preview/scim/v2/Me')['userName']
    folder = '/Users/' + user + '/SafeStepWeeklyReports'
    api('POST', '/api/2.0/workspace/mkdirs', dict(path=folder))
    for filename in ('weekly_report.py', 'databricks_report.py'):
        content = base64.b64encode((HERE / filename).read_bytes()).decode()
        api('POST', '/api/2.0/workspace/import', dict(path=folder + '/' + filename, format='AUTO', content=content, overwrite=True))
    content = base64.b64encode((HERE / 'databricks/weekly_report_notebook.py').read_bytes()).decode()
    api('POST', '/api/2.0/workspace/import', dict(path=folder + '/run_weekly_digest', format='SOURCE', language='PYTHON', content=content, overwrite=True))
    settings = job_settings(folder, config, namespace, active)
    if not state.exists():
        matches = api('GET', '/api/2.1/jobs/list?' + urlencode(dict(name=settings['name']))).get('jobs', [])
        if len(matches) > 1:
            raise ValueError('Multiple report jobs exist; resolve duplicate schedules before deploying')
        if matches:
            candidate = matches[0]
            tasks = candidate.get('settings', {}).get('tasks', [])
            if not tasks:
                tasks = api('GET', '/api/2.1/jobs/get?' + urlencode(dict(job_id=candidate['job_id'])))['settings']['tasks']
            if tasks[0].get('notebook_task', {}).get('notebook_path') != folder + '/run_weekly_digest':
                raise ValueError('An unrelated job already uses the report name')
            state.write_text(json.dumps(dict(job_id=candidate['job_id'])))
            state.chmod(0o600)
    if state.exists():
        job_id = json.loads(state.read_text())['job_id']
        existing = api('GET', '/api/2.1/jobs/get?' + urlencode(dict(job_id=job_id)))
        if existing['settings']['name'] != settings['name']:
            raise ValueError('Deployment state points at a different job')
        api('POST', '/api/2.1/jobs/reset', dict(job_id=job_id, new_settings=settings))
    else:
        job_id = api('POST', '/api/2.1/jobs/create', settings)['job_id']
    state.write_text(json.dumps(dict(job_id=job_id, folder=folder), indent=2) + '\n')
    state.chmod(0o600)
    print(f'Deployed Job {job_id}; schedule ' + ('active' if active else 'paused; preview mode'))
    return job_id


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=HERE / 'reports/config.json')
    p.add_argument('--state', type=Path, default=HERE / 'reports/databricks-deployment.json')
    p.add_argument('--activate', action='store_true')
    p.add_argument('--store-smtp-secret', action='store_true', help='copy exported SMTP_PASSWORD into the private Databricks secret scope')
    p.add_argument('--run-preview', action='store_true')
    args = p.parse_args()
    if args.activate and args.run_preview:
        p.error('Use --run-preview on a paused deployment before --activate')
    if args.store_smtp_secret:
        if not os.environ.get('SMTP_PASSWORD'):
            raise ValueError('Export SMTP_PASSWORD privately before storing the secret')
        scopes = api('GET', '/api/2.0/secrets/scopes/list').get('scopes', [])
        if not any(scope['name'] == 'safestep-email' for scope in scopes):
            api('POST', '/api/2.0/secrets/scopes/create', dict(scope='safestep-email'))
        api('POST', '/api/2.0/secrets/put', dict(scope='safestep-email', key='smtp-password', string_value=os.environ['SMTP_PASSWORD']))
        print('Stored SMTP credential in Databricks Secrets.')
    table = os.environ.get('SAFESTEP_TABLE', 'workspace.safestep.lifecycle_events')
    namespace = table.rsplit('.', 1)[0]
    if len(namespace.split('.')) != 2:
        raise ValueError('SAFESTEP_TABLE must include catalog.schema.table for hosted reports')
    job_id = deploy(json.loads(args.config.read_text()), namespace, args.state, args.activate)
    if args.run_preview:
        result = api('POST', '/api/2.1/jobs/run-now', dict(job_id=job_id, notebook_params=dict(mode='verify')))
        print('Preview run ID: ' + str(result['run_id']))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__)
        raise SystemExit(1) from None
