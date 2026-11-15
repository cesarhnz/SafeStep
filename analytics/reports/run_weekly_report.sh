#!/bin/sh
# Invoke every 15 minutes. Python applies each user's local weekly schedule.
set -eu
REPORT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$REPORT_ROOT"
if [ -f analytics/.env ]; then
    set -a
    . ./analytics/.env
    set +a
fi
exec "$REPORT_ROOT/.venv/bin/python" "$REPORT_ROOT/analytics/weekly_report.py" --send-due "$@"
