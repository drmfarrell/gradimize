#!/usr/bin/env bash
# One-command setup for a fresh clone on Ubuntu. Idempotent — safe to re-run.
#
# Usage:
#   scripts/bootstrap.sh [--locked] [--dev] [--no-web]
#
#   --locked   Install from requirements-lock.txt (pinned versions), then
#              `pip install -e . --no-deps` for the console scripts.
#   --dev      Also install test/lint tooling (pytest, pytest-asyncio, httpx, ruff).
#   --no-web   Skip the npm build (web UI); TUI still works.
set -euo pipefail
cd "$(dirname "$0")/.."

# Bootstrap only ever touches the repo-local sample DB — it must never be able
# to touch a live database. Login shells may export GRADIMIZE_DATA_DIR to the
# live DB location; unset it so the seed/doctor smoke test below resolves to
# the repo-local data/ dir (same guard start.sh and tests/conftest.py use).
unset GRADIMIZE_DATA_DIR

LOCKED=0
DEV=0
NO_WEB=0
for arg in "$@"; do
    case "$arg" in
        --locked)  LOCKED=1 ;;
        --dev)     DEV=1 ;;
        --no-web)  NO_WEB=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
    esac
done

echo "== Python interpreter =="
PYBIN=""
if command -v python3.12 >/dev/null 2>&1; then
    PYBIN="python3.12"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
    PYBIN="python3"
fi
if [ -z "$PYBIN" ]; then
    echo "No suitable Python found (need >= 3.11). Try:"
    echo "  sudo apt install python3.12 python3.12-venv"
    exit 1
fi
echo "Using $PYBIN ($($PYBIN --version))"

echo "== Virtualenv =="
if [ ! -d .venv ]; then
    "$PYBIN" -m venv .venv
fi
.venv/bin/pip install --upgrade pip -q

echo "== Dependencies =="
if [ "$LOCKED" -eq 1 ]; then
    .venv/bin/pip install -r requirements-lock.txt
    .venv/bin/pip install -e . --no-deps
else
    .venv/bin/pip install -e .
fi
if [ "$DEV" -eq 1 ]; then
    .venv/bin/pip install pytest pytest-asyncio httpx ruff
fi

echo "== Web UI =="
if [ "$NO_WEB" -eq 1 ]; then
    echo "Skipping web build (--no-web)."
elif command -v npm >/dev/null 2>&1; then
    npm ci --prefix web
    npm run build --prefix web
else
    echo "Warning: npm not found — skipping web UI build. The TUI still works (./start.sh)."
fi

echo "== TLS certificate =="
if [ ! -f certs/gradimize.crt ]; then
    scripts/make_cert.sh || echo "Warning: cert generation failed — needed only for ./serve.sh --secure"
fi

echo "== Smoke test =="
if [ ! -s data/gradimize.db ]; then
    .venv/bin/python scripts/seed_sample.py
fi
set +e
.venv/bin/python scripts/doctor.py --skip-solver-checks
DOCTOR_EXIT=$?
set -e
if [ "$DOCTOR_EXIT" -eq 2 ]; then
    echo "doctor.py reported a FAIL — bootstrap incomplete."
    exit 2
fi

echo "== Next steps =="
cat <<'EOF'
  ./start.sh                    TUI on synthetic sample data
  ./serve.sh --secure           HTTPS web UI (token in data/access_token.txt)
  ./bugreport.sh                paste-safe support bundle if something breaks
  deploy/gradimize.service      systemd unit for a persistent server
EOF
