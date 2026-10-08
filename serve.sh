#!/usr/bin/env bash
# Gradimize server launcher — API + web UI (localhost/LAN only).
#
# Unlike start.sh (which is TUI-focused and WIPES the database on every run),
# this serves the EXISTING database and exposes the web app.
#
# Modes:
#   dev (default)   API :8000 + Vite dev server :5173 (hot reload, http,
#                   LOCALHOST ONLY — not reachable from the LAN)
#   --secure        ONE hardened process on :8443 — HTTPS, access-token
#                   auth, static production web build. Requires a cert
#                   (./scripts/make_cert.sh) — the mode refuses to start
#                   without one. Token: from $GRADIMIZE_TOKEN, else
#                   data/access_token.txt (generated on first run).
#   --no-viz        Disable the live solver visualization (on by default).
#
# Usage:
#   ./serve.sh                 dev mode against the current DB
#   ./serve.sh --secure        hardened HTTPS mode (use this for previews)
#   ./serve.sh --fresh         wipe the DB and reload data/sample first
#   ./serve.sh --no-viz        disable the live solver visualization
#
# Run it so it survives you disconnecting (recommended):
#   screen -S gradimize ./serve.sh     # then press Ctrl-A then D to detach
#   screen -r gradimize                # reattach later to see logs
#   (inside the screen, press Ctrl-C once to stop both cleanly)

cd "$(dirname "$0")"
# Files this script creates (token, logs) must never be group/world-readable.
umask 077

PYTHON=".venv/bin/python"
# Live DB location. Set GRADIMIZE_DATA_DIR to a path OUTSIDE the repo for
# real-data deployments; it must match what src/database/connection.py uses.
DB_FILE="${GRADIMIZE_DATA_DIR:-data}/gradimize.db"

# Isolation rule: data never in the repo, LOGS never in the data dir.
# Run logs (src/core/runlog.py) resolve to the repo's gitignored logs/
# regardless of GRADIMIZE_DATA_DIR — they are the one channel the AI
# assistant reads, so they must stay outside the FERPA zone. Set
# GRADIMIZE_LOG_DIR only to move them somewhere else that is NOT the zone.
DATA_DIR="data/sample"  # synthetic seed CSVs (always in-repo)
TIME_SLOTS="config/time_slots.yaml"

FRESH=0
SECURE=0
NO_VIZ=0
for arg in "$@"; do
    case "$arg" in
        --fresh)     FRESH=1 ;;
        --secure)    SECURE=1 ;;
        --no-viz)    NO_VIZ=1 ;;
        -h|--help)   sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
    esac
done

if [ "$NO_VIZ" -eq 1 ]; then
    export GRADIMIZE_VIZ=0
fi

if [ ! -f "$PYTHON" ]; then
    echo "Error: venv not found. Run: python3 -m venv .venv && .venv/bin/pip install -e ."
    exit 1
fi

# ── Data: reuse existing DB unless --fresh, or seed if empty/missing ──────────
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
    echo "Using existing database ($(student_count) students). Use --fresh to reload."
fi

# ── Process management: start children, clean up on exit ──────────────────────
PIDS=()
cleanup() {
    echo; echo "Shutting down ..."
    for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
    wait 2>/dev/null
    echo "Stopped."
}
trap cleanup INT TERM EXIT

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

if [ "$SECURE" -eq 1 ]; then
    # ── Hardened mode: one HTTPS process, token auth, static web build ───────
    CERT="certs/gradimize.crt"; KEY="certs/gradimize.key"
    if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
        echo "Error: no TLS certificate. Run ./scripts/make_cert.sh first"
        echo "       (or install one from campus IT as certs/gradimize.{crt,key})."
        exit 1
    fi

    TOKEN_FILE="data/access_token.txt"
    if [ -z "${GRADIMIZE_TOKEN:-}" ]; then
        if [ ! -s "$TOKEN_FILE" ]; then
            "$PYTHON" -c "import secrets; print(secrets.token_urlsafe(24))" > "$TOKEN_FILE"
            chmod 600 "$TOKEN_FILE"
            echo "Generated access token → $TOKEN_FILE"
        fi
        GRADIMIZE_TOKEN="$(cat "$TOKEN_FILE")"
    fi
    export GRADIMIZE_TOKEN

    echo "Building web UI (production bundle)…"
    npm --prefix web run build > /tmp/gradimize_build.log 2>&1 || {
        echo "Web build failed — see /tmp/gradimize_build.log"; exit 1; }

    echo
    echo "─────────────────────────────────────────────────────────────"
    echo "  Gradimize (hardened): HTTPS + access token, LAN only."
    echo "    Local : https://localhost:8443"
    [ -n "$LAN_IP" ] && echo "    LAN   : https://$LAN_IP:8443   (on-campus)"
    echo
    # Never echo the token itself: screen/terminal scrollback persists.
    echo "  Access token: cat $TOKEN_FILE   (distribute out-of-band)"
    echo "  Cert fingerprint (for users to verify the browser warning):"
    openssl x509 -in "$CERT" -noout -fingerprint -sha256 | sed 's/^/    /'
    echo "  Press Ctrl-C to stop."
    echo "─────────────────────────────────────────────────────────────"
    # --no-access-log: request lines include ?token= query params (EventSource
    # and CSV downloads can't set headers), which must not land in scrollback.
    exec "$PYTHON" -m uvicorn src.api.main:app --host 0.0.0.0 --port 8443 \
        --no-access-log \
        --ssl-certfile "$CERT" --ssl-keyfile "$KEY"
fi

# ── Dev mode: API + Vite dev server ──────────────────────────────────────────
echo "Starting API  → http://127.0.0.1:8000"
"$PYTHON" -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000 \
    > /tmp/gradimize_api.log 2>&1 &
PIDS+=($!)

# Dev mode is deliberately localhost-only (no TLS, no token): vite.config.ts
# binds 127.0.0.1. LAN serving happens exclusively via --secure.
echo "Starting web  → http://127.0.0.1:5173  (localhost only)"
npm --prefix web run dev > /tmp/gradimize_vite.log 2>&1 &
PIDS+=($!)

# Wait for the web server to answer before printing the banner.
for _ in $(seq 1 30); do
    curl -s -o /dev/null -m 2 http://127.0.0.1:5173/ && break
    sleep 1
done

echo
echo "─────────────────────────────────────────────────────────────"
echo "  Gradimize is running (dev mode, localhost/LAN only)."
echo "    Local : http://localhost:5173"
[ -n "$LAN_IP" ] && echo "    LAN   : http://$LAN_IP:5173   (on-campus)"
echo
echo "  For demos/previews use HTTPS+auth instead:  ./serve.sh --secure"
echo "  Logs: /tmp/gradimize_{api,vite}.log"
echo "  Press Ctrl-C to stop everything."
echo "─────────────────────────────────────────────────────────────"

wait
