#!/usr/bin/env python3
"""Publish SafeStep and schedule Databricks-managed snapshot email. No SMTP credentials."""
import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse, urlencode

try:
    from .deploy_report_job import api
    from .weekly_report import validate_config
except ImportError:
    from deploy_report_job import api
    from weekly_report import validate_config

HERE = Path(__file__).resolve().parent
NAME = 'SafeStep Weekly Dashboard Email'
OLD_NAME = 'SafeStep Weekly Caregiver Report'


def snapshot_settings(config, dashboard_id, warehouse_id):
    config = validate_config(config)
    if not re.fullmatch(r'[a-fA-F0-9]{32}', dashboard_id):
        raise ValueError('Use a Databricks dashboard ID')
    if not warehouse_id or not config['recipients']:
        raise ValueError('Choose a SQL warehouse and at least one recipient')
    hour, minute = map(int, config['time'].split(':'))
    return dict(name=NAME, max_concurrent_runs=1,
                schedule=dict(quartz_cron_expression=f'0 {minute} {hour} ? * {config["weekday"][:3].upper()}',
                              timezone_id=config['timezone'], pause_status='UNPAUSED'),
                tasks=[dict(task_key='dashboard_snapshot', timeout_seconds=1200, max_retries=0,
                            dashboard_task=dict(dashboard_id=dashboard_id, warehouse_id=warehouse_id,
                                                subscription=dict(paused=False, custom_subject='SafeStep | Weekly dashboard snapshot',
                                                                  subscribers=[dict(user_name=address) for address in config['recipients']])) )])


def deploy(config, dashboard_id, state, publish=False, run_now=False):
    config = validate_config(config)
    endpoint = '/api/2.0/lakeview/dashboards/' + dashboard_id
    dashboard = api('GET', endpoint)
    if publish:
        # Retain a private rollback copy, and change only the outdated incident source.
        backup = state.with_name('dashboard-before-native-email.json')
        if not backup.exists():
            backup.write_text(json.dumps(dashboard, indent=2) + '\n')
            backup.chmod(0o600)
        definition = json.loads(dashboard['serialized_dashboard'])
        table = os.getenv('SAFESTEP_TABLE', 'workspace.safestep.lifecycle_events')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){2}', table):
            raise ValueError('Use a catalog.schema.table source')
        namespace = table.rsplit('.', 1)[0]
        for dataset in definition['datasets']:
            if dataset['name'] == 'safestep_incidents':
                dataset['config']['source'] = 'SELECT * FROM ' + namespace + '.incidents'
        api('PATCH', endpoint, dict(serialized_dashboard=json.dumps(definition), etag=dashboard['etag']))
        api('POST', endpoint + '/published', dict(embed_credentials=False, warehouse_id=dashboard['warehouse_id']))
    else:
        api('GET', endpoint + '/published')  # Must be published before configuring delivery.
    settings = snapshot_settings(config, dashboard_id, dashboard['warehouse_id'])
    old_state = state.parent / 'databricks-deployment.json'
    existing_state = state if state.exists() else old_state if old_state.exists() else None
    if existing_state:
        job_id = json.loads(existing_state.read_text())['job_id']
        existing = api('GET', '/api/2.1/jobs/get?' + urlencode(dict(job_id=job_id)))
        if existing['settings']['name'] not in (NAME, OLD_NAME):
            raise ValueError('Deployment state points to an unrelated job')
        api('POST', '/api/2.1/jobs/reset', dict(job_id=job_id, new_settings=settings))
    else:
        matches = api('GET', '/api/2.1/jobs/list?' + urlencode(dict(name=NAME))).get('jobs', [])
        if len(matches) > 1:
            raise ValueError('Multiple dashboard email jobs found; resolve duplicate schedules')
        if matches:
            job_id = matches[0]['job_id']
            api('POST', '/api/2.1/jobs/reset', dict(job_id=job_id, new_settings=settings))
        else:
            job_id = api('POST', '/api/2.1/jobs/create', settings)['job_id']
    result = dict(job_id=job_id, dashboard_id=dashboard_id, transport='databricks_snapshot')
    # Persist before starting the one-off email so deployment retries don't obscure its run ID.
    state.write_text(json.dumps(result, indent=2) + '\n')
    state.chmod(0o600)
    if run_now:
        run = api('POST', '/api/2.1/jobs/run-now', dict(job_id=job_id))
        result['run_id'] = run['run_id']
        state.write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=HERE / 'reports/config.json')
    p.add_argument('--state', type=Path, default=HERE / 'reports/dashboard-email-deployment.json')
    p.add_argument('--publish', action='store_true', help='update incident source and publish the existing dashboard')
    p.add_argument('--send-now', action='store_true', help='send one snapshot immediately in addition to enabling the weekly schedule')
    args = p.parse_args()
    config = validate_config(json.loads(args.config.read_text()))
    match = re.search(r'/dashboards(?:v3)?/([a-fA-F0-9]{32})', urlparse(config['dashboard_url']).path)
    if not match:
        raise ValueError('Configure the SafeStep dashboard URL first')
    args.state.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps(deploy(config, match[1], args.state, args.publish, args.send_now)))


if __name__ == '__main__':
    main()
