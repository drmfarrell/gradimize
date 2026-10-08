#!/usr/bin/env bash
# Consistent, dated backup of the Gradimize database (safe under WAL while
# the server runs — uses SQLite's online backup API via Python; the sqlite3
# CLI is not required).
#
# Usage:  ./scripts/backup_db.sh [backup-dir]     (default: backups/)
# Cron:   0 2 * * *  /path/to/gradimize/scripts/backup_db.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# Must match where the app puts the live DB (GRADIMIZE_DATA_DIR, same as
# serve.sh and src/database/connection.py) — otherwise cron backs up nothing.
DB="${GRADIMIZE_DATA_DIR:-data}/gradimize.db"
DEST="${1:-backups}"
KEEP=30
PYTHON=".venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="python3"

# Exit NON-zero when there is nothing to back up: a silent success from cron
# would look like working backups while protecting nothing.
[ -f "$DB" ] || { echo "ERROR: no database at $DB — nothing backed up." >&2; exit 1; }
mkdir -p "$DEST"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$DEST/gradimize_$STAMP.db"

"$PYTHON" - "$DB" "$OUT" << 'PYEOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
print(f"Backed up {src} -> {dst}")
PYEOF
gzip "$OUT"
echo "Compressed to $OUT.gz"

# Retention: keep the newest $KEEP
ls -1t "$DEST"/gradimize_*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
