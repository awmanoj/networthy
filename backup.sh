#!/usr/bin/env bash
#
# backup.sh — take a consistent snapshot of Networthy's SQLite database.
#
# Uses SQLite's online backup API (via the app container's bundled Python) so the
# copy is consistent even if the app is writing at the time — safer than tar-ing
# the live .db file. Backups are gzipped into $BACKUP_DIR, and old ones beyond the
# retention count are pruned.
#
# Usage (on the server running the container):
#   ./backup.sh
#
# Cron (every 4 hours) — `crontab -e`:
#   0 */4 * * * /full/path/to/backup.sh >> /var/log/networthy-backup.log 2>&1
#   # if cron can't find docker, add at the top of the crontab:
#   #   PATH=/usr/local/bin:/usr/bin:/bin
#
# Tunables (env):
#   CONTAINER_NAME  container to back up            (default: networthy)
#   DB_PATH         DB path inside the container     (default: /app/data/networthy.db)
#   BACKUP_DIR      where snapshots are written       (default: $HOME/networthy-backups)
#   KEEP            how many snapshots to retain      (default: 42 ≈ 7 days at every 4h)

set -euo pipefail

CONTAINER="${CONTAINER_NAME:-networthy}"
DB_PATH="${DB_PATH:-/app/data/networthy.db}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/networthy-backups}"
KEEP="${KEEP:-42}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# The container must be up to take an online backup.
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "ERROR: container '$CONTAINER' is not running — skipping backup."
  exit 1
fi

mkdir -p "$BACKUP_DIR"
ts="$(date +%Y%m%d-%H%M%S)"
tmp_in="/tmp/networthy-backup-$ts.db"          # inside the container
out_db="$BACKUP_DIR/networthy-$ts.db"          # on the host, before gzip

# 1) Consistent online backup to a temp file inside the container.
docker exec "$CONTAINER" python -c \
  'import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()' "$DB_PATH" "$tmp_in"

# 2) Copy it out, compress, and clean up the in-container temp.
docker cp "$CONTAINER:$tmp_in" "$out_db"
docker exec "$CONTAINER" rm -f "$tmp_in"
gzip -f "$out_db"                              # -> $out_db.gz

log "backup written: ${out_db}.gz ($(du -h "${out_db}.gz" | cut -f1))"

# 3) Retention — keep the newest $KEEP, delete older ones.
mapfile -t old < <(ls -1t "$BACKUP_DIR"/networthy-*.db.gz 2>/dev/null | tail -n +"$((KEEP + 1))")
if ((${#old[@]})); then
  rm -f "${old[@]}"
  log "pruned ${#old[@]} old backup(s), keeping newest ${KEEP}."
fi
