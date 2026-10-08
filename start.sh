#!/usr/bin/env bash
# Gradimize start script — reuses the existing repo-local sample DB, wipes
# and reloads synthetic sample data only with --fresh (or if the DB is empty).
# Usage: ./start.sh [tui|api] [--fresh] [-- extra args for tui/api]

set -e

cd "$(dirname "$0")"

# This script only ever touches the repo-local sample DB — it must never be
# able to touch the live database. Login shells export GRADIMIZE_DATA_DIR to
# the live DB location; unset it here so get_engine() below resolves to the
# repo-local data/ dir no matter how this script was invoked (same guard
# tests/conftest.py uses).
unset GRADIMIZE_DATA_DIR

# Use the project venv
PYTHON=".venv/bin/python"
if [ ! -f "$PYTHON" ]; then
    echo "Error: Virtual environment not found. Run 'python3 -m venv .venv && .venv/bin/pip install -e .' first."
    exit 1
fi

DATA_DIR="data/sample"
TIME_SLOTS="config/time_slots.yaml"
DB_FILE="data/gradimize.db"

FRESH=0
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --fresh) FRESH=1 ;;
        *)       ARGS+=("$arg") ;;
    esac
done
set -- "${ARGS[@]}"
MODE="${1:-tui}"

seed_data() {
    echo "Loading data from $DATA_DIR ..."
    "$PYTHON" scripts/seed_sample.py --data-dir "$DATA_DIR" --time-slots "$TIME_SLOTS"
}

student_count() {
    "$PYTHON" -c "
from sqlmodel import Session, select, func
from src.database.connection import get_engine
from src.database.models import Student
try:
    with Session(get_engine()) as s:
        print(s.exec(select(func.count()).select_from(Student)).one())
except Exception:
    print(0)" 2>/dev/null
}

if [ "$FRESH" -eq 1 ]; then
    echo "--fresh: wiping $DB_FILE"
    rm -f "$DB_FILE"
    seed_data
elif [ ! -s "$DB_FILE" ] || [ "$(student_count)" = "0" ]; then
    echo "Database is empty or missing — seeding from $DATA_DIR."
    seed_data
else
    echo "Using existing database. Use --fresh to reload sample data."
fi

case "$MODE" in
    tui)
        [ $# -gt 0 ] && shift
        echo "Starting TUI ..."
        $PYTHON -m src.tui.app "$@"
        ;;
    api)
        echo "Starting API server at http://127.0.0.1:8000 ..."
        $PYTHON -m src.api.main
        ;;
    *)
        echo "Unknown mode: $MODE"
        echo "Usage: ./start.sh [tui|api] [--fresh]"
        exit 1
        ;;
esac
