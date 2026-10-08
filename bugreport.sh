#!/usr/bin/env bash
# Collect a paste-safe support bundle (wraps scripts/bug_report.py so you
# don't have to remember the .venv python). All flags pass through, e.g.:
#   ./bugreport.sh
#   ./bugreport.sh --no-doctor     # if the database itself is broken
set -euo pipefail
cd "$(dirname "$0")"

PYTHON=".venv/bin/python"
if [ ! -f "$PYTHON" ]; then
    echo "Error: venv not found. Run scripts/bootstrap.sh first." >&2
    exit 1
fi

exec "$PYTHON" scripts/bug_report.py "$@"
