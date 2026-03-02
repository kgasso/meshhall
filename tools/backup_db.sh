#!/usr/bin/env bash
# =============================================================================
# MeshHall Database Backup Script
# =============================================================================
# Usage:
#   bash tools/backup_db.sh                     # backup to default location
#   bash tools/backup_db.sh --dest /mnt/nas/backups
#   bash tools/backup_db.sh --db /opt/meshhall/data/meshhall.db --dest /tmp
#   bash tools/backup_db.sh --keep 14           # keep 14 days of backups
#
# Safe to run while MeshHall is live — uses SQLite's .backup command which
# copies an atomic, consistent snapshot even while the database is being written.
#
# Cron example (daily at 2am, keep 30 days):
#   0 2 * * * /opt/meshhall/venv/bin/python /opt/meshhall/tools/backup_db.py \
#             --db /opt/meshhall/data/meshhall.db \
#             --dest /opt/meshhall/data/backups \
#             --keep 30
# =============================================================================

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
DB_PATH="/opt/meshhall/data/meshhall.db"
DEST_DIR="/opt/meshhall/data/backups"
KEEP_DAYS=30

# ── Colour helpers ─────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'
    YELLOW='\033[1;33m'; RESET='\033[0m'
else
    GREEN=''; CYAN=''; RED=''; YELLOW=''; RESET=''
fi

info()  { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()    { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
die()   { error "$*"; exit 1; }

# ── Argument parsing ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)        DB_PATH="$2";   shift ;;
        --db=*)      DB_PATH="${1#*=}" ;;
        --dest)      DEST_DIR="$2";  shift ;;
        --dest=*)    DEST_DIR="${1#*=}" ;;
        --keep)      KEEP_DAYS="$2"; shift ;;
        --keep=*)    KEEP_DAYS="${1#*=}" ;;
        --help|-h)
            echo "Usage: bash backup_db.sh [--db PATH] [--dest DIR] [--keep DAYS]"
            echo ""
            echo "  --db PATH     Path to meshhall.db  (default: $DB_PATH)"
            echo "  --dest DIR    Backup destination    (default: $DEST_DIR)"
            echo "  --keep DAYS   Days of backups to retain (default: $KEEP_DAYS, 0=keep all)"
            exit 0 ;;
        *) die "Unknown argument: $1" ;;
    esac
    shift
done

# ── Pre-flight ─────────────────────────────────────────────────────────────────
[ -f "$DB_PATH" ] || die "Database not found: $DB_PATH"

# Find python3/sqlite3 — prefer the meshhall venv if available
PYTHON=""
for p in "/opt/meshhall/venv/bin/python3" "/opt/meshhall/venv/bin/python" \
          "$(which python3 2>/dev/null)" "$(which python 2>/dev/null)"; do
    if [ -x "$p" ] 2>/dev/null; then
        PYTHON="$p"
        break
    fi
done
[ -n "$PYTHON" ] || die "python3 not found — cannot perform safe SQLite backup"

mkdir -p "$DEST_DIR"

# ── Backup ─────────────────────────────────────────────────────────────────────
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_FILE="$DEST_DIR/meshhall-${TIMESTAMP}.db"

info "Source:  $DB_PATH"
info "Dest:    $BACKUP_FILE"

# Use SQLite's built-in online backup API via Python — safe while bot is running.
# This creates an atomic, consistent copy even if the DB is being written to.
"$PYTHON" - << PYEOF
import sqlite3, sys

src_path  = "$DB_PATH"
dest_path = "$BACKUP_FILE"

try:
    src  = sqlite3.connect(src_path)
    dest = sqlite3.connect(dest_path)
    with dest:
        src.backup(dest, pages=256, progress=None)
    dest.close()
    src.close()
except Exception as e:
    print(f"Backup failed: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF

# Verify the backup is a valid SQLite database
"$PYTHON" -c "
import sqlite3, sys
try:
    conn = sqlite3.connect('$BACKUP_FILE')
    conn.execute('PRAGMA integrity_check').fetchone()
    conn.close()
except Exception as e:
    print(f'Backup integrity check failed: {e}', file=sys.stderr)
    sys.exit(1)
" || { rm -f "$BACKUP_FILE"; die "Backup verification failed — file removed."; }

BACKUP_SIZE="$(du -sh "$BACKUP_FILE" | cut -f1)"
ok "Backup created: $BACKUP_FILE ($BACKUP_SIZE)"

# ── Prune old backups ──────────────────────────────────────────────────────────
if [ "$KEEP_DAYS" -gt 0 ]; then
    PRUNED=$(find "$DEST_DIR" -maxdepth 1 -name "meshhall-*.db" \
                  -mtime +"$KEEP_DAYS" -print -delete | wc -l)
    if [ "$PRUNED" -gt 0 ]; then
        info "Pruned $PRUNED backup(s) older than ${KEEP_DAYS} days."
    fi
fi

# ── Summary ────────────────────────────────────────────────────────────────────
TOTAL=$(find "$DEST_DIR" -maxdepth 1 -name "meshhall-*.db" | wc -l)
ok "Done. $TOTAL backup(s) in $DEST_DIR"
